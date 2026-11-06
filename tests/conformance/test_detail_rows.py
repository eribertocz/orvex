"""P2-24 (#346): detail pages at the base dataset's grain, in one stable order, in keyset pages
on one snapshot, with row and column policies, within the budgets that can be enforced."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.correctness.discharge import DischargeError, verify_physical, verify_plan
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.result.cursors import SealedForm
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_conformed as conformed
from tests.conformance import test_row_policies as rp
from tests.conformance.test_execution import ask, tiny_lake
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
Rows = list[list[str | None]]
FORM = SealedForm(b"k" * 32)
FIELDS = ("sales.sale_id", "sales.region", "customers.country", "sales.amount")


def query(
    size: int = 2,
    order: list[Document] | None = None,
    fields: tuple[str, ...] = FIELDS,
    **more: Any,  # noqa: ANN401
) -> Document:
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": f} for f in fields],
        "order": order or [{"by": "sales.amount", "direction": "desc"}],
        "page": {"size": str(size)},
    } | more


def policies(*rules: Document, **more: Any) -> PolicyModel:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, "row_policies": list(rules)} | more),
        rp.COMPILED,
        groups=frozenset(),
        managed_tables=rp.MANAGED,
    )


class Pager:
    """Every page of one question on one lake, as `server` would serve them: the snapshot the
    first page read stays pinned for the cursors it issues."""

    def __init__(
        self, lake: Lake, policy: PolicyModel | None = None, context: Document | None = None
    ) -> None:
        self.lake = lake
        self.snapshot = ce.engine_for(lake).snapshot
        self.policy, self.context = policy, context
        self.reads: list[Any] = []

    def engine(self) -> dict[str, Any]:
        def counted(request: Any) -> Any:  # noqa: ANN401
            self.reads.append(request)
            return self.lake.read(request)

        def pinned(version: str) -> Mapping[str, object] | None:
            return self.snapshot if version == str(self.snapshot["id"]) else None

        return {"cursors": FORM, "pinned": pinned, "read": counted, "snapshot": self.snapshot}

    def page(self, document: Document, after: str | None = None) -> Any:  # noqa: ANN401
        if after is not None:
            document = document | {"page": {**document["page"], "after": after}}
        return ask(
            document,
            rp.COMPILED,
            self.lake,
            policy=self.policy,
            context=self.context,
            **self.engine(),
        )

    def all(self, document: Document) -> tuple[Rows, int]:
        found = self.page(document)
        rows, pages = ce.rows_of(found), 1
        while "next" in found.result.envelope()["page"]:
            found = self.page(document, found.result.envelope()["page"]["next"])
            rows += ce.rows_of(found)
            pages += 1
        return rows, pages


# --- the base grain, the order and the pages ---------------------------------------------------


def test_every_row_once_in_one_stable_order_across_pages() -> None:
    with rp.lake_of() as lake:
        rows, pages = Pager(lake).all(query())
    assert rows == [
        ["3", "North", "BO", "999.00"],
        ["1", "North", "BO", "100.00"],
        ["2", "South", "PE", "50.00"],
        ["4", "North", None, "7.00"],  # no customer: its row is kept, the field is NULL
    ]
    assert pages == 2


Sale = tuple[str, str | None, int]  # tenant, customer, cents


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from("AB"), st.sampled_from(["1", "2", "9", None]), st.integers(-999, 999)
        ),
        min_size=1,
        max_size=9,
    ),
    st.integers(1, 4),
    st.sampled_from(["asc", "desc"]),
    st.sampled_from(["first", "last"]),
)
def test_pages_concatenate_to_every_authorized_row_exactly_once(
    sales: list[Sale], size: int, direction: str, nulls: str
) -> None:
    data = {
        "sales": [
            [str(n), t, "1", c or "", "North", f"{Decimal(v) / 100:.2f}"]
            for n, (t, c, v) in enumerate(sales)
        ],
        "customers": [["1", "BO"], ["2", "PE"]],  # customer 9 has no row: a NULL country
    }
    order = [
        {"by": "customers.country", "direction": direction, "nulls": nulls},
        {"by": "sales.amount"},
    ]
    context = rp.caller(tenant="A")
    with rp.lake_of(data=data) as lake:
        rows, _ = Pager(lake, policies(rp.TENANT), context).all(query(size, order))
    countries = {"1": "BO", "2": "PE"}
    expected = [
        [str(n), "North", countries.get(c or ""), f"{Decimal(v) / 100:.2f}"]
        for n, (t, c, v) in enumerate(sales)
        if t == "A"
    ]

    def key(row: list[str | None]) -> tuple[object, ...]:
        country = row[2]
        place = (country is None) != (nulls == "last")
        text = country or ""
        ranked = (
            text if direction == "asc" else "".join(chr(0x10FFFF - ord(ch)) for ch in text) + "￿"
        )
        return (
            not place,
            ranked if country is not None else "",
            Decimal(str(row[3])),
            int(str(row[0])),
        )

    assert rows == sorted(expected, key=key)


def test_every_page_reads_the_first_page_s_snapshot() -> None:
    with rp.lake_of() as lake:
        pager = Pager(lake)
        first = pager.page(query(size=1))
        with lake.transaction() as transaction:  # new data arrives between two pages
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql="INSERT INTO \"sales\" VALUES (5, 'A', 1, 1, 'North', 5000.00)",
                    parameters=[],
                )
            )
        rest = []
        found = first
        while "next" in found.result.envelope()["page"]:
            found = pager.page(query(size=1), found.result.envelope()["page"]["next"])
            rest += ce.rows_of(found)
        fresh = ce.rows_of(ask(query(size=10), rp.COMPILED, lake))
    assert ce.rows_of(first) == [["3", "North", "BO", "999.00"]]
    assert [r[0] for r in rest] == ["1", "2", "4"]  # the new sale is not on a later page
    assert fresh[0][0] == "5"  # a new question sees it


# --- cursors ----------------------------------------------------------------------------------


def refused(pager: Pager, document: Document, cursor: str) -> dict[str, Any]:
    before = len(pager.reads)
    with pytest.raises(SemanticError) as caught:
        pager.page(document, cursor)
    assert caught.value.code == "PAGE_CURSOR_INVALID"
    assert len(pager.reads) == before  # refused before any read
    return dict(caught.value.details)


def test_a_cursor_works_only_for_its_own_question_caller_and_page_size() -> None:
    with rp.lake_of() as lake:
        pager = Pager(lake)
        cursor = pager.page(query()).result.envelope()["page"]["next"]
        tampered = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]
        assert refused(pager, query(), tampered) == {"reason": "invalid"}
        assert refused(pager, query(size=3), cursor) == {"reason": "invalid"}
        other = query(order=[{"by": "sales.amount"}])
        assert refused(pager, other, cursor) == {"reason": "invalid"}
        stranger = Pager(
            lake, context=ce.viewer_context() | {"permission_fingerprint": "sha256:" + "f" * 64}
        )
        stranger.snapshot = pager.snapshot
        assert refused(stranger, query(), cursor) == {"reason": "invalid"}
        assert refused(pager, query(), "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") == {
            "reason": "invalid"
        }


def test_a_cursor_whose_snapshot_is_no_longer_kept_is_expired() -> None:
    with rp.lake_of() as lake:
        pager = Pager(lake)
        cursor = pager.page(query()).result.envelope()["page"]["next"]
        kept = pager.snapshot
        pager.snapshot = {
            **kept,
            "id": int(str(kept["id"])) + 1,
        }  # a later snapshot, the old one gone
        assert refused(pager, query(), cursor) == {"reason": "expired"}


def test_a_cursor_never_shows_what_it_holds() -> None:
    with rp.lake_of() as lake:
        cursor = Pager(lake).page(query()).result.envelope()["page"]["next"]
    assert "100" not in cursor  # sealed: the position is never visible
    assert "BO" not in cursor


# --- grain, joins and policies -----------------------------------------------------------------


def test_fields_no_one_path_reaches_many_to_one_are_refused_before_any_read() -> None:
    document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "sales.amount"}, {"member": "refunds.amount"}],
        "order": [{"by": "sales.amount"}],
        "page": {"size": "5"},
    }
    reads: list[Any] = []
    with conformed.lake_of(conformed.DATA) as lake, pytest.raises(SemanticError) as caught:
        ask(document, conformed.COMPILED, lake, read=lambda r: reads.append(r))
    assert caught.value.code == "NO_RELATIONSHIP_PATH"
    assert reads == []


def test_the_finer_dataset_is_the_base_and_the_coarser_is_joined() -> None:
    fields = ("customers.country", "sales.sale_id")
    with rp.lake_of() as lake:
        found = ask(
            query(size=10, fields=fields, order=[{"by": "sales.sale_id"}]), rp.COMPILED, lake
        )
    assert ce.rows_of(found) == [["BO", "1"], ["PE", "2"], ["BO", "3"], [None, "4"]]  # per sale


def test_a_dataset_without_a_key_has_no_detail_page() -> None:
    document = {
        "version": "0.2.0.dev0",
        "name": "nokey",
        "datasets": [
            {"name": "t", "source": "t", "fields": [{"name": "a", "expression": {"sql": "a"}}]}
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    catalog = {"t": {"a": SourceColumn("BIGINT")}}
    compiled = compile_model(
        read(json.dumps(document).encode(), "semantic/n.ossie.json", VENDORS), catalog, VENDORS
    )
    detail = query(size=5, fields=("t.a",), order=[{"by": "t.a"}])
    with tiny_lake(catalog, [{"a": "1"}]) as lake, pytest.raises(SemanticError) as caught:
        ask(detail, compiled, lake)
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "detail_key"},
    )


def test_row_policies_restrict_the_base_and_every_joined_dataset() -> None:
    country = {
        "name": "own_country",
        "dataset": "customers",
        "kind": "predicate",
        "predicate": "country = $country",
    }
    with rp.lake_of() as lake:
        rows, _ = Pager(
            lake, policies(rp.TENANT, country), rp.caller(tenant="A", country="BO")
        ).all(query())
    assert rows == [
        ["1", "North", "BO", "100.00"]
    ]  # tenant B's sale, PE's sale and the orphan hidden


MASKED = {"member": "sales.region", "default": "masked", "mask": "###"}


def test_a_masked_field_shows_only_its_mask_and_is_never_read() -> None:
    with rp.lake_of() as lake:
        found = ask(query(size=10), rp.COMPILED, lake, policy=policies(column_policies=[MASKED]))
    assert [row[1] for row in ce.rows_of(found)] == ["###"] * 4
    assert '"REGION"' not in found.physical.sql
    (column,) = [c for c in found.result.envelope()["columns"] if c["name"] == "region"]
    assert (column["masked"], column["mask"]) == (True, "###")


@pytest.mark.parametrize(
    ("change", "operation"),
    [
        ({"order": [{"by": "sales.region"}]}, "sort"),
        ({"filters": [rp.region("North")]}, "filter"),
    ],
)
def test_a_masked_field_is_never_sorted_or_filtered_on(change: Document, operation: str) -> None:
    reads: list[Any] = []
    with rp.lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(
            query() | change,
            rp.COMPILED,
            lake,
            policy=policies(column_policies=[MASKED]),
            read=lambda r: reads.append(r),
        )
    assert (caught.value.code, dict(caught.value.details)) == (
        "MEMBER_MASKED",
        {"member": "sales.region", "operation": operation},
    )
    assert reads == []


def test_a_hidden_field_is_not_found() -> None:
    hidden = {"member": "sales.amount", "default": "hidden"}
    with rp.lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(query(), rp.COMPILED, lake, policy=policies(column_policies=[hidden]))
    assert caught.value.code == "MEMBER_NOT_FOUND"


# --- budgets ----------------------------------------------------------------------------------


def test_a_page_above_the_cap_is_refused_when_bound() -> None:
    with rp.lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(query(size=1001), rp.COMPILED, lake)
    assert (caught.value.code, dict(caught.value.details)) == (
        "QUERY_BUDGET_EXCEEDED",
        {"budget": "result_rows", "limit": "1000"},
    )


class Stopped(Exception):
    def __init__(self, code: str, details: dict[str, str]) -> None:
        super().__init__(code)
        self.code, self.details = code, details


@pytest.mark.parametrize(
    ("error", "limits", "budget"),
    [
        (
            Stopped("STORE_TIMEOUT", {}),
            {"timeout_ms": 5000},
            {"budget": "time_ms", "limit": "5000"},
        ),
        (
            Stopped("STORE_QUERY_FAILED", {"duckdb": "Out of Memory Error: failed to allocate"}),
            {"memory_bytes": 1 << 30},
            {"budget": "memory_bytes", "limit": str(1 << 30)},
        ),
    ],
)
def test_a_read_stopped_by_a_limit_is_refused_whole(
    error: Stopped, limits: dict[str, Any], budget: dict[str, str]
) -> None:
    def stopped(request: Any) -> Any:  # noqa: ANN401
        raise error

    with rp.lake_of() as lake, pytest.raises(SemanticError) as caught:
        ask(query(), rp.COMPILED, lake, read=stopped, **limits)
    assert (caught.value.code, dict(caught.value.details)) == ("QUERY_BUDGET_EXCEEDED", budget)
    with rp.lake_of() as lake, pytest.raises(Stopped):  # an unknown limit is not a budget
        ask(query(), rp.COMPILED, lake, read=stopped)


# --- evidence and receipt ---------------------------------------------------------------------


def test_the_receipt_records_the_page_but_never_its_values() -> None:
    with rp.lake_of() as lake:
        found = Pager(lake, policies(rp.TENANT), rp.caller(tenant="A")).page(query())
    document = json.dumps(found.receipt.document())
    assert "999.00" not in document
    assert "100.00" not in document
    recorded = {
        o["obligation"]: o["evidence"] for o in found.receipt.document()["plan"]["obligations"]
    }
    assert recorded == {"C1.rows:sales": "rows:sales", "C9.policy:sales": "source:sales"}


def test_a_detail_plan_or_statement_that_breaks_its_grain_never_runs() -> None:
    with rp.lake_of() as lake:
        found = ask(query(size=10), rp.COMPILED, lake, policy=policies(column_policies=[MASKED]))
    plan = found.plan
    for broken in (
        replace(plan, order=plan.order[:-1]),  # the key no longer ends the order
        replace(plan, mode="aggregate"),
        replace(plan, masks=()),  # a masked field read as its value
        replace(plan, page=None),
    ):
        with pytest.raises(DischargeError):
            verify_plan(broken)
    for sql in (
        found.physical.sql.replace("LIMIT 11", "LIMIT 1000"),
        found.physical.sql.replace('CAST($p0 AS VARCHAR) AS "region"', '"t0"."REGION" AS "region"'),
    ):
        assert sql != found.physical.sql
        with pytest.raises(DischargeError):
            verify_physical(plan, replace(found.physical, sql=sql))
