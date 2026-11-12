"""The aggregate-table route (C8, P2-25) under row policies (#441 AC5): an answer is the same,
rows, order and exact values, with the table enabled and with it removed, for a restricted
caller, an exempt caller and a caller with no attribute; a table is read only when its proof
holds. This is the only substitution route before P5 (P5-09 to P5-11); P5-11's matcher must pass
these cases unchanged.

The table and its model are `test_materialization`'s (`sales` by month and category, `rollup`
storing revenue and quantity at that grain). "Removed" is the same lake with no verified
evidence, so the proof cannot be used and the base is read. Policies propagated along a link,
kept or not, are proven by `test_materialization_propagation` with the same equivalence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from orvex.semantic.engines import Routing
from orvex.semantic.policies import read_policies

from conformance.runners import corpus_execution as ce
from tests.conformance import test_direct_query as dq
from tests.conformance import test_materialization as m
from tests.conformance.test_execution import ask

Document = dict[str, Any]
EXEMPT = m.CATEGORY | {"exempt_groups": ["auditors"]}
ON_TENANT = {  # a policy on a field that is not in the table's grain
    "name": "own_tenant",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "tenant = $tenant",
}


def policy(*rules: Document) -> Any:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, "row_policies": list(rules)}),
        m.COMPILED,
        groups=frozenset({"auditors"}),
        managed_tables={},
    )


CALLERS = {
    "restricted": ce.viewer_context(
        (), {"category": {"type": "text", "value": "A"}, "tenant": {"type": "text", "value": "x"}}
    ),
    "exempt": ce.viewer_context(("auditors",), {}),
    "no-attribute": ce.viewer_context(),
}


def both(document: Document, rules: tuple[Document, ...], caller: str) -> tuple[Any, Any]:
    """The answer with the table usable, and with it removed (no verified evidence)."""
    found = policy(*rules)
    used = m.run(document, m.SAMPLE, policy=found, context=CALLERS[caller])
    base = m.run(document, m.SAMPLE, policy=found, context=CALLERS[caller], verified=False)
    assert "rollup" not in m.receipt_reads(base)
    assert ce.rows_of(used) == ce.rows_of(base)  # the table never changes the answer
    return used, base


SHAPES = {  # (question, the table is read)
    "grain-policy": (m.query("revenue"), True),
    "ratio": (m.query("price"), True),
    "ratio-and-sum": (m.query("revenue", "price"), True),
    "distinct-count": (m.query("customers"), False),
    # Totals are not a shape the proof covers yet ("not eligible: shape"): the base is read.
    "grand-total": (m.query("revenue", totals="grand"), False),
    "rollup-totals": (m.query("revenue", totals="rollup"), False),
}


@pytest.mark.parametrize("caller", sorted(CALLERS))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_table_gives_the_base_answer_for_every_caller(shape: str, caller: str) -> None:
    document, reads_table = SHAPES[shape]
    used, _ = both(document, (EXEMPT,), caller)
    assert ("rollup" in m.receipt_reads(used)) == reads_table, m.evidence(used)
    if caller == "restricted":
        assert all(row[1] in {"A", None} for row in ce.rows_of(used))  # never category B


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_a_policy_off_the_grain_keeps_the_table_unused_for_every_caller(caller: str) -> None:
    used, _ = both(
        m.query("revenue"), (EXEMPT, ON_TENANT | {"exempt_groups": ["auditors"]}), caller
    )
    if caller == "restricted":  # the policy splits the table's groups: the base is read
        assert "rollup" not in m.receipt_reads(used)
        assert m.evidence(used)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"
    else:  # exempt, or no attribute (no row at all): the table may answer, the same rows
        assert ce.rows_of(used) == ([] if caller == "no-attribute" else ce.rows_of(used))


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_a_ratio_whose_component_is_not_stored_reads_the_base(caller: str) -> None:
    revenue_only = m.model(
        m.table("rollup", ["month", "category"], [("r", "SUM(revenue)", "revenue")])
    )
    found = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [EXEMPT]}),
        revenue_only,
        groups=frozenset({"auditors"}),
        managed_tables={},
    )
    context = CALLERS[caller]
    used = m.run(m.query("price"), m.SAMPLE, revenue_only, policy=found, context=context)
    base = m.run(
        m.query("price"), m.SAMPLE, revenue_only, verified=False, policy=found, context=context
    )
    assert m.receipt_reads(used) == ["sales"]
    assert ce.rows_of(used) == ce.rows_of(base)


def test_a_dataset_on_another_engine_never_reads_a_managed_aggregate_table(
    tmp_path: Path,
) -> None:
    """ADR-0027: another engine reads its own tables. A managed-lake aggregate table, verified
    or not, is never read in their place: the other engine answers from the base, the same rows."""
    data = m.lake_rows(m.SAMPLE)
    paths = []
    for name in ("sales", "rollup"):
        columns = list(m.CATALOG[name])
        lines = [",".join(columns)] + [
            ",".join(r.get(c) or "" for c in columns) for r in data[name]
        ]
        path = tmp_path / f"{name}.csv"
        path.write_text("\n".join(lines) + "\n", "utf-8")
        paths.append(path)
    found = policy(EXEMPT)
    context = CALLERS["restricted"]
    with ce.case_lake(paths, dict(m.CATALOG)) as lake:
        managed = ask(
            m.query("revenue"), m.COMPILED, lake, policy=found, context=context,
            evidence=m.checked(m.COMPILED, lake),
        )  # fmt: skip
        assert m.receipt_reads(managed) == ["rollup"]  # on the managed lake, the table answers
        adapter = dq.Simulated(lake)
        routed = ask(
            m.query("revenue"), m.COMPILED, lake, policy=found, context=context,
            evidence=m.checked(m.COMPILED, lake), routing=Routing({"sales": dq.ENTRY}),
            adapters={adapter.connection: adapter},
        )  # fmt: skip
    assert m.receipt_reads(routed) == ["sales"]
    assert all('"rollup"' not in statement for statement, _, _ in adapter.statements)
    assert adapter.statements  # the other engine did answer
    assert ce.rows_of(routed) == ce.rows_of(managed)
