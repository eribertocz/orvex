"""P2-21 (#341): row policies placed before aggregation at every read, with the caller's
attributes from the validated security context only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import InvalidContext, PolicyModel, read_policies
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_asof_windows as windows
from tests.conformance import test_conformed as conformed
from tests.conformance import test_period_end as period
from tests.conformance.test_execution import Spy, ask, tiny_lake
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
Rows = list[list[str | None]]

CATALOG = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "tenant_id": SourceColumn("VARCHAR", False),
        "zone_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT", True),
        "region": SourceColumn("VARCHAR", False),
        "amount": SourceColumn("DECIMAL(18,2)", False),
    },
    "customers": {
        "customer_id": SourceColumn("BIGINT", False),
        "country": SourceColumn("VARCHAR", False),
    },
}
MANAGED = {
    "security.user_zone": ("user_id", "zone_id"),
    "security.registered": ("user_id",),
    "security.org_closure": ("ancestor", "descendant"),
}


def model() -> CompiledModel:
    document = {
        "version": "0.2.0.dev0",
        "name": "tenants",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": [next(iter(columns))],
                "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
            }
            for t, columns in CATALOG.items()
        ],
        "relationships": [
            {
                "name": "buyer",
                "from": "sales",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/tenants.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


COMPILED = model()


def policies(
    *rules: Document,
    groups: frozenset[str] = frozenset(),
    compiled: CompiledModel = COMPILED,
    **more: Any,  # noqa: ANN401
) -> PolicyModel:
    return read_policies(
        yaml.safe_dump({"version": 1, "row_policies": list(rules)} | more),
        compiled,
        groups=groups,
        managed_tables=MANAGED,
    )


TENANT = {
    "name": "own_tenant",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "tenant_id = $tenant",
}
DATA = {
    "sales": [
        ["1", "A", "1", "1", "North", "100.00"],
        ["2", "A", "2", "2", "South", "50.00"],
        ["3", "B", "1", "1", "North", "999.00"],
        ["4", "A", "1", "", "North", "7.00"],
    ],
    "customers": [["1", "BO"], ["2", "PE"]],
}
MAPPINGS = {
    "security.user_zone": [("u-1", 1)],
    "security.registered": [("u-1",)],
}


@contextmanager
def lake_of(
    data: dict[str, list[list[str]]] = DATA, mappings: dict[str, Any] = MAPPINGS
) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(CATALOG[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, CATALOG) as lake:
            with lake.transaction() as transaction:
                transaction.execute(
                    StatementRequest(kind="statement", sql="CREATE SCHEMA security", parameters=[])
                )
                for table, values in mappings.items():
                    columns = MANAGED[table]
                    kinds = ", ".join(
                        f'"{c}" {"BIGINT" if c == "zone_id" else "VARCHAR"}' for c in columns
                    )
                    name = ".".join(f'"{p}"' for p in table.split("."))
                    transaction.execute(
                        StatementRequest(
                            kind="statement", sql=f"CREATE TABLE {name} ({kinds})", parameters=[]
                        )
                    )
                    for row in values:
                        written = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in row)
                        transaction.execute(
                            StatementRequest(
                                kind="statement",
                                sql=f"INSERT INTO {name} VALUES ({written})",
                                parameters=[],
                            )
                        )
            yield lake


def caller(groups: tuple[str, ...] = (), **attributes: Any) -> dict[str, Any]:  # noqa: ANN401
    typed = {
        name: {"type": "list", "values": list(v)}
        if isinstance(v, list)
        else {"type": "integer", "value": str(v)}
        if isinstance(v, int)
        else {"type": "text", "value": v}
        for name, v in attributes.items()
    }
    found = ce.viewer_context(groups, typed)
    found["user"] = "u-1"
    return found


def query(group_by: tuple[str, ...] = ("sales.region",), **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    return found | more


def rows(
    policy: PolicyModel,
    context: dict[str, Any],
    document: Document | None = None,
    **lake: Any,  # noqa: ANN401
) -> Rows:
    with lake_of(**lake) as opened:
        return ce.rows_of(
            ask(document or query(), COMPILED, opened, policy=policy, context=context)
        )


def region(*values: str) -> Document:
    return {
        "scope": "board",
        "member": "sales.region",
        "predicate": {"op": "in", "values": [{"type": "text", "value": v} for v in values]},
    }


# --- placement --------------------------------------------------------------------------------


def test_the_policy_restricts_rows_before_aggregation() -> None:
    assert rows(policies(TENANT), caller(tenant="A")) == [["North", "107.00"], ["South", "50.00"]]
    assert rows(policies(TENANT), caller(tenant="B")) == [["North", "999.00"]]


def test_a_filter_only_restricts_further() -> None:
    found = rows(policies(TENANT), caller(tenant="A"), query(filters=[region("North", "South")]))
    assert found == [["North", "107.00"], ["South", "50.00"]]
    narrowed = rows(policies(TENANT), caller(tenant="A"), query(filters=[region("North")]))
    assert narrowed == [["North", "107.00"]]


def test_a_mapping_table_restricts_in_the_same_statement() -> None:
    zone = {
        "name": "own_zone",
        "dataset": "sales",
        "kind": "predicate",
        "predicate": "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)",
    }
    with lake_of() as lake:
        found = ask(query(), COMPILED, lake, policy=policies(zone), context=caller(user_id="u-1"))
    assert ce.rows_of(found) == [["North", "1106.00"]]  # zone 1 only
    assert '"security"."user_zone"' in found.physical.sql  # one statement, one snapshot


def test_a_policy_on_a_dimension_hides_its_facts_and_their_orphans() -> None:
    country = {
        "name": "own_country",
        "dataset": "customers",
        "kind": "predicate",
        "predicate": "country = $country",
    }
    assert rows(policies(country), caller(country="BO")) == [["North", "1099.00"]]
    visible = country | {"orphans": "visible"}  # sale 4 has no customer
    assert rows(policies(visible), caller(country="BO")) == [["North", "1106.00"]]


# --- attributes -------------------------------------------------------------------------------


def test_attributes_are_bound_never_written_into_the_statement() -> None:
    with lake_of() as lake:
        found = ask(
            query(), COMPILED, lake, policy=policies(TENANT), context=caller(tenant="O'Brien")
        )
    assert ce.rows_of(found) == []
    assert "Brien" not in found.physical.sql
    assert {"type": "VARCHAR", "value": "O'Brien"} in [
        {"type": p["type"], "value": p["value"]} for p in found.physical.parameters
    ]


def test_a_missing_attribute_gives_zero_rows_and_a_generic_notice() -> None:
    with lake_of() as lake:
        found = ask(query(), COMPILED, lake, policy=policies(TENANT), context=caller())
    assert ce.rows_of(found) == []
    (notice,) = found.result.envelope()["notices"]
    assert notice["code"] == "POLICY_ATTRIBUTE_MISSING"
    assert "own_tenant" not in json.dumps(notice)  # the generic view names no policy


def test_a_client_can_never_supply_an_attribute() -> None:
    # A filter value that looks like an attribute is only text, and only restricts further.
    looks = {
        "scope": "board",
        "member": "sales.tenant_id",
        "predicate": {"op": "in", "values": [{"type": "text", "value": "$tenant"}]},
    }
    assert rows(policies(TENANT), caller(tenant="A"), query(filters=[looks])) == []
    # The query has no member for attributes at all: anything else is refused before a read.
    spy = Spy()
    with lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(
            query() | {"attributes": {"tenant": "B"}},
            COMPILED,
            lake,
            policy=policies(TENANT),
            context=caller(tenant="A"),
            read=spy,
        )
    assert caught.value.code == "QUERY_INVALID"
    assert spy.calls == 0


def test_an_invalid_context_is_refused_before_any_read() -> None:
    forged = caller(tenant="A")
    forged["attributes"]["tenant"] = {"type": "float", "value": "1.5"}
    spy = Spy()
    with lake_of() as lake, pytest.raises(InvalidContext):
        ask(query(), COMPILED, lake, policy=policies(TENANT), context=forged, read=spy)
    assert spy.calls == 0


# --- deny-lists and exemptions ----------------------------------------------------------------

DENY = {
    "name": "not_blocked_zone",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "zone_id NOT IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)",
    "registration": {"table": "security.registered", "key": "user_id"},
}


def test_a_registered_caller_sees_all_but_the_denied_rows() -> None:
    assert rows(policies(DENY), caller(user_id="u-1")) == [["South", "50.00"]]


def test_an_unregistered_caller_sees_zero_rows_with_a_notice() -> None:
    with lake_of(mappings={"security.user_zone": [], "security.registered": []}) as lake:
        found = ask(query(), COMPILED, lake, policy=policies(DENY), context=caller(user_id="u-1"))
    assert ce.rows_of(found) == []
    assert [n["code"] for n in found.result.envelope()["notices"]] == ["POLICY_MAPPING_MISSING"]


def test_an_exempt_group_sees_every_row_of_that_policy_only() -> None:
    exempt = TENANT | {"exempt_groups": ["auditors"]}
    policy = policies(exempt, groups=frozenset({"auditors"}))
    with lake_of() as lake:
        found = ask(
            query(), COMPILED, lake, policy=policy, context=caller(("auditors",), tenant="A")
        )
    assert ce.rows_of(found) == [["North", "1106.00"], ["South", "50.00"]]
    used = found.receipt.document()["security"]["policies"]
    assert [(p["name"], p["applied"]) for p in used] == [("own_tenant", "exempt")]
    builder = caller(tenant="A")
    builder["role"] = "builder"  # a role never exempts
    assert rows(policy, builder) == [["North", "107.00"], ["South", "50.00"]]


# --- non-broadening ---------------------------------------------------------------------------


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from("AB"),
            st.sampled_from(["North", "South", "East"]),
            st.integers(-9999, 9999),
        ),
        min_size=1,
        max_size=8,
    ),
    st.sampled_from("AB"),
    st.sets(st.sampled_from(["North", "South", "East"]), min_size=1),
)
def test_no_filter_ever_widens_what_the_policy_allows(
    sales: list[tuple[str, str, int]], tenant: str, shown: set[str]
) -> None:
    data = {
        "sales": [
            [str(n), t, "1", "1", r, f"{Decimal(c) / 100:.2f}"] for n, (t, r, c) in enumerate(sales)
        ],
        "customers": [["1", "BO"]],
    }
    found = rows(
        policies(TENANT), caller(tenant=tenant), query(filters=[region(*sorted(shown))]), data=data
    )
    expected: dict[str, Decimal] = {}
    for t, r, c in sales:
        if t == tenant and r in shown:
            expected[r] = expected.get(r, Decimal(0)) + Decimal(c) / 100
    assert found == [[r, f"{expected[r]:.2f}"] for r in sorted(expected)]


# --- the other read sites ---------------------------------------------------------------------

REGION = {"name": "own_region", "kind": "predicate", "predicate": "region = $region"}


def test_the_period_end_ranks_only_the_authorized_snapshots() -> None:
    compiled, catalog = period.snapshots_model()
    policy = policies(REGION | {"dataset": "t"}, compiled=compiled)
    data = period.rows((1, "2026-01-10", "North", "100.00"), (1, "2026-01-20", "South", "999.00"))
    document = period.query("balance", group_by=[{"member": "t.day", "bucket": {"unit": "month"}}])
    with tiny_lake(catalog, data) as lake:
        found = ask(document, compiled, lake, policy=policy, context=caller(region="North"))
    assert ce.rows_of(found) == [["2026-01-01", "100.00"]]  # never the hidden later snapshot
    assert found.physical.sql.count('"#p"') >= 2  # restricted in the ranking and in the read


def test_the_as_of_guard_counts_only_the_authorized_history() -> None:
    policy = policies(REGION | {"dataset": "customer_history"}, compiled=windows.HISTORY)
    document = windows.query("revenue", group_by=windows.BY_REGION)
    data = {
        "sales": [["1", "1", "2026-01-15", "100.00"]],
        "customer_history": [
            ["1", "North", "2026-01-01", "2026-02-01"],
            ["1", "South", "2026-01-01", "2026-02-01"],  # overlaps, but hidden
        ],
    }
    with windows.lake_of(data, windows.HISTORY_CATALOG) as lake:
        found = ask(document, windows.HISTORY, lake, policy=policy, context=caller(region="North"))
    assert ce.rows_of(found) == [["North", "100.00"]]
    assert found.physical.as_of is not None


def test_the_running_total_accumulates_only_the_authorized_rows() -> None:
    policy = policies(REGION | {"dataset": "daily"}, compiled=windows.DAILY)
    document = windows.query("cumulative", group_by=("daily.day",))
    with windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake:
        found = ask(document, windows.DAILY, lake, policy=policy, context=caller(region="South"))
    assert ce.rows_of(found) == [["2026-01-02", "10.00"], ["2026-01-03", "15.00"]]


# --- still refused ----------------------------------------------------------------------------

HIERARCHY = {
    "name": "org",
    "dataset": "sales",
    "kind": "hierarchy",
    "closure": {
        "table": "security.org_closure",
        "ancestor": "ancestor",
        "descendant": "descendant",
    },
    "node": "org_node",
    "field": "zone_id",
}
CELL = {
    "member": "sales.region",
    "default": "visible",
    "mask": "*",
    "visible_when": "zone_id = $zone_id",
}


@pytest.mark.parametrize(
    ("policy", "operation"),
    [((HIERARCHY,), "hierarchy_policy"), ((), "cell_policy")],
)
def test_hierarchies_and_conditional_cells_stay_refused_before_any_read(
    policy: tuple[Document, ...], operation: str
) -> None:
    more: dict[str, Any] = {} if policy else {"column_policies": [CELL]}
    spy = Spy()
    with lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(
            query(("sales.sale_id",)),
            COMPILED,
            lake,
            policy=policies(*policy, **more),
            context=caller(),
            read=spy,
        )
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": operation},
    )
    assert spy.calls == 0


# --- the receipt ------------------------------------------------------------------------------


class Viewer:
    builder = False

    def __init__(self, known: frozenset[str]) -> None:
        self.known = known

    def sees_member(self, member: str) -> bool:
        return True

    def sees_value(self, member: str, value: object) -> bool:
        return True

    def sees_dataset(self, dataset: str) -> bool:
        return True

    def knows_policy(self, name: str) -> bool:
        return name in self.known


def test_the_receipt_shows_an_exemption_only_to_a_viewer_who_may_know_the_policy() -> None:
    exempt = TENANT | {"exempt_groups": ["auditors"]}
    policy = policies(exempt, groups=frozenset({"auditors"}))
    with lake_of() as lake:
        found = ask(
            query(), COMPILED, lake, policy=policy, context=caller(("auditors",), tenant="A")
        )
    told = found.receipt.view(Viewer(frozenset({"own_tenant"})))["security"]
    assert [(p["name"], p["applied"]) for p in told["policies"]] == [("own_tenant", "exempt")]
    untold = found.receipt.view(Viewer(frozenset()))["security"]
    assert untold["policies"] == []
    assert "own_tenant" not in json.dumps(found.receipt.view())


CHANNEL = {"kind": "predicate", "predicate": "channel = $channel"}
FACT = st.lists(
    st.tuples(
        st.sampled_from(["web", "shop"]),
        st.sampled_from(["North", "South"]),
        st.integers(-9999, 9999),
    ),
    max_size=6,
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    FACT,
    FACT,
    st.sampled_from(["web", "shop"]),
    st.sets(st.sampled_from(["North", "South"]), min_size=1),
)
def test_aligned_facts_and_metrics_over_them_use_authorized_rows_only(
    sales: list[tuple[str, str, int]],
    refunds: list[tuple[str, str, int]],
    channel: str,
    shown: set[str],
) -> None:
    def table(found: list[tuple[str, str, int]]) -> list[list[str]]:
        return [
            [str(n), "2026-01-01", r, c, f"{Decimal(a) / 100:.2f}"]
            for n, (c, r, a) in enumerate(found)
        ]

    def total(found: list[tuple[str, str, int]]) -> dict[str, Decimal]:
        sums: dict[str, Decimal] = {}
        for c, r, a in found:
            if c == channel and r in shown:
                sums[r] = sums.get(r, Decimal(0)) + Decimal(a) / 100
        return sums

    policy = policies(
        CHANNEL | {"name": "own_sales", "dataset": "sales"},
        CHANNEL | {"name": "own_refunds", "dataset": "refunds"},
        compiled=conformed.COMPILED,
    )
    document = conformed.query(
        "gross_revenue", "refunds", "net_revenue", filters=[region(*sorted(shown))]
    )
    data = {"sales": table(sales), "refunds": table(refunds), "targets": []}
    with conformed.lake_of(data) as lake:
        found = ask(
            document, conformed.COMPILED, lake, policy=policy, context=caller(channel=channel)
        )
    gross, back = total(sales), total(refunds)

    def shown_as(value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"

    expected = [
        [
            "2026-01-01",
            r,
            shown_as(gross.get(r)),
            shown_as(back.get(r)),
            shown_as(gross[r] - back[r] if r in gross and r in back else None),
        ]
        for r in sorted(set(gross) | set(back))
    ]
    assert ce.rows_of(found) == expected
