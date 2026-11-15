"""P3-14 (#448; #442/#478, Owner): a period-end balance computed over an entity key the caller
cannot see shows nothing of that key.

The model is `test_period_end`'s: account snapshots (`t`), a period-end `balance` added across
`account`, whose `entity_keys` field `account_id` is hidden or masked for the caller.

- **P6, observable invariance.** Renaming the accounts by any bijection, which keeps them apart,
  changes nothing the caller observes: the columns, the rows in their order, the notices, and
  the receipt view without the fields that differ between two runs of the very same question.
  It shows the key's values are hidden. It does not show that nothing can be inferred from the
  aggregates themselves: a cell with one account shows that account's balance (Q1 = A).
- **P7, unreachable.** A question that groups, filters, sorts or counts by the key is refused
  before any read: `MEMBER_NOT_FOUND` when it is hidden, `MEMBER_MASKED` when it is masked.
- **P8, oracle.** The balance equals a plain-Python reference over the rows the caller may see,
  the row policy applied before the period-end selection.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from orvex.contracts.store.client import Lake
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt.viewer import CallerViewer

from conformance.runners import corpus_execution as ce
from tests.conformance.test_period_end import snapshots_model
from tests.properties.world import PROFILE

COMPILED, CATALOG = snapshots_model()
Row = dict[str, str | None]
DAYS = ("2026-01-05", "2026-01-20", "2026-01-31", "2026-02-03", "2026-02-27", "2026-04-01")
REGIONS = ("N", "S", None)
BALANCES = (None, "0.00", "-3.50", "10.00", "99999.99")
RENAMED = ("101", "205", "307", "999999")  # any distinct ids: the key's values never matter


def policies(rule: str, row_policy: bool) -> PolicyModel:
    document: dict[str, Any] = {"version": 1, "column_policies": [
        {"member": "t.account_id", "default": rule, **({"mask": "***"} if rule == "masked" else {})}
    ]}  # fmt: skip
    if row_policy:
        document["row_policies"] = [
            {"name": "own_region", "dataset": "t", "kind": "predicate",
             "predicate": "region = $region"}
        ]  # fmt: skip
    return read_policies(yaml.safe_dump(document), COMPILED, groups=frozenset(), managed_tables={})


def context(region: str | None) -> dict[str, Any]:
    attributes = {} if region is None else {"region": {"type": "text", "value": region}}
    return ce.viewer_context(attributes=attributes)


@st.composite
def snapshots(draw: st.DrawFn) -> list[Row]:
    """Up to four accounts, each in one region, with at most one snapshot a day."""
    accounts = draw(st.integers(1, 4))
    found: list[Row] = []
    for account in range(1, accounts + 1):
        region = draw(st.sampled_from(REGIONS))
        for day in draw(st.lists(st.sampled_from(DAYS), min_size=1, max_size=4, unique=True)):
            found.append({
                "snapshot_id": str(len(found) + 1), "account_id": str(account), "day": day,
                "region": region, "customer": None, "balance": draw(st.sampled_from(BALANCES)),
            })  # fmt: skip
    return found


@st.composite
def questions(draw: st.DrawFn) -> dict[str, Any]:
    """Questions that vary the grain, the period and the filters on visible members; each needs
    its time axis, and none filters on it (period-end rules)."""
    unit = draw(st.sampled_from(("day", "month", "year")))
    group_by: list[dict[str, Any]] = [{"member": "t.day", "bucket": {"unit": unit}}]
    if draw(st.booleans()):
        group_by.insert(0, {"member": "t.region"})
    query: dict[str, Any] = {
        "kind": "semantic_query", "version": 1, "mode": "aggregate",
        "metrics": [{"metric": "balance"}], "group_by": group_by,
        "order": [{"by": entry["member"], "direction": "asc"} for entry in group_by],
    }  # fmt: skip
    if draw(st.booleans()):
        query["filters"] = [{
            "scope": "visual", "member": "t.region",
            "predicate": {"op": "in", "values": [{"type": "text", "value": "N"}],
                          "include_null": draw(st.booleans())},
        }]  # fmt: skip
    return query


@contextmanager
def lake_of(data: list[Row]) -> Iterator[Lake]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "t.csv"
        names = list(CATALOG["t"])
        lines = [",".join(names)]
        lines += [",".join("" if r[n] is None else str(r[n]) for n in names) for r in data]
        path.write_text("\n".join(lines) + "\n", "utf-8")
        with ce.case_lake([path], CATALOG) as lake:
            yield lake


def ask(
    query: dict[str, Any], data: list[Row], model: PolicyModel, caller: dict[str, Any]
) -> Answer:
    with lake_of(data) as lake:
        return answer(json.dumps(query), COMPILED, model, caller, ce.engine_for(lake))


def observed(found: Answer, model: PolicyModel, caller: dict[str, Any]) -> dict[str, Any]:
    envelope = found.result.envelope()
    return {
        "columns": envelope["columns"],
        "rows": ce.rows_of(found),
        "notices": envelope.get("notices", []),
        "receipt": found.receipt.view(CallerViewer(COMPILED, model, caller)),
    }


def stable(one: Any, two: Any) -> Any:  # noqa: ANN401
    """`one` without what differs from `two`, a run of the very same question: what changes
    between two runs legitimately (ids, snapshots, fingerprints, times) is left out."""
    if isinstance(one, Mapping) and isinstance(two, Mapping):
        return {k: stable(v, two[k]) for k, v in one.items() if k in two and _same(v, two[k])}
    if isinstance(one, list) and isinstance(two, list) and len(one) == len(two):
        return [stable(a, b) for a, b in zip(one, two, strict=True)]
    return one


def _same(one: Any, two: Any) -> bool:  # noqa: ANN401
    if isinstance(one, Mapping) and isinstance(two, Mapping):
        return True  # compared field by field
    if isinstance(one, list) and isinstance(two, list):
        return len(one) == len(two)
    return bool(one == two)


def renamed(data: list[Row], order: list[int]) -> list[Row]:
    """The same snapshots with each account given another id, one for one."""
    names = {str(i + 1): RENAMED[at] for i, at in enumerate(order)}
    return [{**row, "account_id": names[str(row["account_id"])]} for row in data]


@PROFILE
@given(
    snapshots(),
    questions(),
    st.sampled_from(("hidden", "masked")),
    st.booleans(),
    st.sampled_from(("N", "S", None)),
    st.permutations(range(4)),
)
def test_renaming_the_hidden_key_changes_nothing_the_caller_observes(
    data: list[Row],
    query: dict[str, Any],
    rule: str,
    row_policy: bool,
    region: str | None,
    order: list[int],
) -> None:
    """P6."""
    model, caller = policies(rule, row_policy), context(region)
    first = observed(ask(query, data, model, caller), model, caller)
    again = observed(ask(query, data, model, caller), model, caller)
    other = observed(ask(query, renamed(data, list(order)), model, caller), model, caller)
    assert stable(other, again) == stable(first, again)
    for value in {row["account_id"] for row in renamed(data, list(order))} | {"1", "2", "3"}:
        assert f'"{value}"' not in json.dumps(other["rows"]), value


KEYED = [
    ("group", {"group_by": [{"member": "t.account_id"},
                            {"member": "t.day", "bucket": {"unit": "month"}}]}),
    ("filter", {"filters": [{"scope": "visual", "member": "t.account_id",
                             "predicate": {"op": "is_not_null"}}]}),
    ("order", {"order": [{"by": "t.account_id", "direction": "asc"}]}),
]  # fmt: skip


@pytest.mark.parametrize("rule", ["hidden", "masked"])
@pytest.mark.parametrize(("how", "change"), KEYED, ids=[k for k, _ in KEYED])
def test_a_question_by_the_hidden_key_is_refused_before_any_read(
    rule: str, how: str, change: dict[str, Any]
) -> None:
    """P7: grouping, filtering and sorting by the key; counting by it is a metric the model
    would have to declare, which the inherited protection hides (`security-policies` v1)."""
    query: dict[str, Any] = {
        "kind": "semantic_query", "version": 1, "mode": "aggregate",
        "metrics": [{"metric": "balance"}],
        "group_by": [{"member": "t.day", "bucket": {"unit": "month"}}], **change,
    }  # fmt: skip
    data = [{"snapshot_id": "1", "account_id": "1", "day": "2026-01-05", "region": "N",
             "customer": None, "balance": "1.00"}]  # fmt: skip
    model = policies(rule, False)
    with lake_of(data) as lake:
        reads: list[object] = []
        reading = lake.read

        def counted(*given: Any, **named: Any) -> Any:  # noqa: ANN401
            reads.append(given)
            return reading(*given, **named)

        lake.read = counted  # type: ignore[method-assign]
        with pytest.raises(SemanticError) as caught:
            answer(json.dumps(query), COMPILED, model, context(None), ce.engine_for(lake))
    expected = "MEMBER_NOT_FOUND" if rule == "hidden" else "MEMBER_MASKED"
    assert caught.value.code == expected, how
    assert reads == []


def reference(
    data: list[Row], query: dict[str, Any], region: str | None, row_policy: bool
) -> list[list[str | None]]:
    """P8's oracle: each account's latest snapshot in each period, among the rows the caller
    may see, summed per group; a `NULL` latest adds nothing, and a group of `NULL`s is `NULL`."""
    seen = [r for r in data if not row_policy or (region is not None and r["region"] == region)]
    for entry in query.get("filters", ()):
        allowed = {v["value"] for v in entry["predicate"]["values"]}
        seen = [r for r in seen if r["region"] in allowed
                or (r["region"] is None and entry["predicate"]["include_null"])]  # fmt: skip
    entries = query["group_by"]
    unit = entries[-1]["bucket"]["unit"]

    def period(day: str) -> str:
        found = date.fromisoformat(day)
        if unit == "month":
            found = found.replace(day=1)
        elif unit == "year":
            found = found.replace(month=1, day=1)
        return found.isoformat()

    latest: dict[tuple[Any, ...], tuple[str, str | None]] = {}
    for row in seen:
        key = (
            *(row["region"] for e in entries if e["member"] == "t.region"),
            period(str(row["day"])),
            row["account_id"],
        )
        if key not in latest or str(row["day"]) > latest[key][0]:
            latest[key] = (str(row["day"]), row["balance"])
    totals: dict[tuple[Any, ...], Decimal | None] = {}
    for key, (_, balance) in latest.items():
        group = key[:-1]
        before = totals.get(group)
        if balance is None:
            totals.setdefault(group, None)
        else:
            totals[group] = (before or Decimal(0)) + Decimal(balance)
    rows = [[*group, None if total is None else f"{total:.2f}"] for group, total in totals.items()]
    return sorted(rows, key=lambda r: [(v is None, v or "") for v in r[:-1]])


@PROFILE
@given(snapshots(), questions(), st.sampled_from(("hidden", "masked")), st.booleans(),
       st.sampled_from(("N", "S", None)))  # fmt: skip
def test_the_balance_is_the_reference_over_the_rows_the_caller_may_see(
    data: list[Row], query: dict[str, Any], rule: str, row_policy: bool, region: str | None
) -> None:
    """P8."""
    found = ask(query, data, policies(rule, row_policy), context(region))
    assert ce.rows_of(found) == reference(data, query, region, row_policy)


def test_the_invariance_check_catches_a_key_that_shows() -> None:
    """P6 can fail: an answer that depends on the key's values differs once they are renamed."""
    one = {"rows": [["1", "10.00"]], "receipt": {"id": "a"}}
    again = {"rows": [["1", "10.00"]], "receipt": {"id": "b"}}
    shown = {"rows": [["205", "10.00"]], "receipt": {"id": "c"}}
    assert stable(shown, again) != stable(one, again)
    assert stable({"rows": [["1", "10.00"]], "receipt": {"id": "c"}}, again) == stable(one, again)
