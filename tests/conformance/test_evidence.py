"""P2-26 (#351): evidence checked on one snapshot, bound to it and to its declaration, and what a
question's plan relies on: violated evidence refuses it before any read."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from orvex.contracts.store.v1 import ChangesRequest, StatementRequest
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness import analyze
from orvex.semantic.errors import SemanticError
from orvex.semantic.evidence import EvidenceRecord, Ledger, declarations, relied, verify
from orvex.semantic.ossie.reader import read
from orvex.semantic.planning import plan
from orvex.semantic.query.binding import Limits, bind_query
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_materialization as rollups
from tests.conformance.test_execution import Spy, ask, no_policies
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
Data = dict[str, list[list[str]]]
AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

CATALOG = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT", False),
        "sale_date": SourceColumn("DATE", False),
        "amount": SourceColumn("DECIMAL(18,2)", False),
    },
    "customers": {
        "customer_id": SourceColumn("BIGINT", False),
        "region": SourceColumn("VARCHAR", False),
    },
    "profiles": {
        "customer_id": SourceColumn("BIGINT", False),
        "tier": SourceColumn("VARCHAR", False),
    },
    "customer_history": {
        "customer_id": SourceColumn("BIGINT", False),
        "segment": SourceColumn("VARCHAR", False),
        "valid_from": SourceColumn("DATE", False),
        "valid_to": SourceColumn("DATE", False),
    },
}
KEYS = {
    "sales": ["sale_id"],
    "customers": ["customer_id"],
    "profiles": ["customer_id"],
    "customer_history": ["customer_id", "valid_from"],
}
DATASET_PAYLOADS: dict[str, Document] = {
    "sales": {"evidence": ["unique", "append_only"]},
    "customers": {"evidence": ["unique"]},
    "customer_history": {
        "time_roles": {"validity": {"from": "valid_from", "to": "valid_to"}},
        "evidence": ["non_overlap"],
    },
}


def relationship(name: str, source: str, target: str, **payload: Any) -> Document:  # noqa: ANN401
    return {
        "name": name,
        "from": source,
        "to": target,
        "from_columns": ["customer_id"],
        "to_columns": ["customer_id"],
        "custom_extensions": [extension({"element": "relationship", **payload})],
    }


def document(**changes: Document) -> Document:
    """The model, with each dataset's payload in `changes` in place of its own."""
    payloads = DATASET_PAYLOADS | changes
    temporal = {
        "event_time": "sales.sale_date",
        "valid_from": "customer_history.valid_from",
        "valid_to": "customer_history.valid_to",
        "boundary": "closed_open",
        "match": "exactly_one",
    }
    return {
        "version": "0.2.0.dev0",
        "name": "evidence",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": KEYS[t],
                "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
            }
            | (
                {"custom_extensions": [extension({"element": "dataset", **payloads[t]})]}
                if t in payloads
                else {}
            )
            for t, columns in CATALOG.items()
        ],
        "relationships": [
            relationship(
                "sale_customer",
                "sales",
                "customers",
                cardinality="many_to_one",
                evidence=["cardinality", "referential_integrity"],
            ),
            relationship(
                "customer_profile",
                "customers",
                "profiles",
                cardinality="one_to_one",
                evidence=["cardinality"],
            ),
            relationship(
                "customer_at_sale",
                "sales",
                "customer_history",
                cardinality="many_to_one",
                temporal=temporal,
                evidence=["cardinality"],
            ),
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }


def compiled_of(found: Document) -> CompiledModel:
    loaded = read(json.dumps(found).encode(), "semantic/evidence.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


COMPILED = compiled_of(document())

GOOD: Data = {
    "sales": [
        ["1", "10", "2026-01-05", "10.00"],
        ["2", "11", "2026-01-06", "5.00"],
        ["3", "10", "2026-02-01", "2.50"],
    ],
    "customers": [["10", "North"], ["11", "South"]],
    "profiles": [["10", "gold"], ["11", "silver"]],
    "customer_history": [
        ["10", "retail", "2026-01-01", "2026-02-01"],
        ["10", "business", "2026-02-01", "2027-01-01"],
        ["11", "retail", "2026-01-01", "2027-01-01"],
    ],
}


@contextmanager
def lake_of(data: Data, catalog: dict[str, Any] = CATALOG) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(catalog[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, {t: catalog[t] for t in data}) as lake:
            yield lake


def with_rows(**more: list[list[str]]) -> Data:
    return {t: rows + more.get(t, []) for t, rows in GOOD.items()}


def outcomes(data: Data, compiled: CompiledModel = COMPILED) -> dict[tuple[str, str], Any]:
    with lake_of(data) as lake:
        found = verify(compiled, lake.read, lake.current_snapshot().id, at=AT, run="run-1")
    result: dict[tuple[str, str], Any] = {
        (r.name.lower(), r.kind): (r.status, dict(r.violations)) for r in found.records
    }
    result |= {(s.declaration.name.lower(), s.declaration.kind): s.reason for s in found.skipped}
    return result


def query(group_by: str) -> Document:
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
        "group_by": [{"member": group_by}],
    }


def records_of(compiled: CompiledModel, lake: Lake) -> Any:  # noqa: ANN401
    snapshot = lake.current_snapshot().id
    found = verify(compiled, lake.read, snapshot, at=AT, run="run-1").records
    return lambda s: found if s["id"] == snapshot else ()


# --- each kind, verified and violated, counted exactly ---------------------------------------

VERIFIED = {
    ("sales", "unique"): ("verified", {"duplicates": 0}),
    ("sales", "append_only"): "first",  # no earlier snapshot to compare against (#355)
    ("customers", "unique"): ("verified", {"duplicates": 0}),
    ("customer_history", "non_overlap"): ("verified", {"overlaps": 0}),
    ("sale_customer", "cardinality"): ("verified", {"repeated": 0}),
    ("sale_customer", "referential_integrity"): ("verified", {"orphans": 0}),
    ("customer_profile", "cardinality"): ("verified", {"repeated": 0}),
    ("customer_at_sale", "cardinality"): ("verified", {"overlaps": 0}),
}


def test_every_declared_kind_is_verified_on_correct_data() -> None:
    """`append_only` with no earlier snapshot of the dataset to compare against stays
    `declared`, never `verified` on a guess (#355)."""
    assert outcomes(GOOD) == VERIFIED


@pytest.mark.parametrize(
    ("more", "violated"),
    [
        (  # a duplicated key: the dataset's `unique`, and the relationship reaching it
            {"customers": [["10", "West"]]},
            {
                ("customers", "unique"): {"duplicates": 1},
                ("sale_customer", "cardinality"): {"repeated": 1},
                ("customer_profile", "cardinality"): {"repeated": 1},  # its `from` side
            },
        ),
        (  # two duplicated keys, one of them three times: keys counted, not rows
            {"sales": [["1", "10", "2026-01-05", "1.00"], ["1", "10", "2026-01-05", "1.00"]]}
            | {"customers": [["11", "West"]]},
            {
                ("sales", "unique"): {"duplicates": 1},
                ("customers", "unique"): {"duplicates": 1},
                ("sale_customer", "cardinality"): {"repeated": 1},
                ("customer_profile", "cardinality"): {"repeated": 1},  # its `from` side
            },
        ),
        (  # an overlapping validity: one pair, for the dataset and its temporal relationship
            {"customer_history": [["11", "business", "2026-03-01", "2026-04-01"]]},
            {
                ("customer_history", "non_overlap"): {"overlaps": 1},
                ("customer_at_sale", "cardinality"): {"overlaps": 1},
            },
        ),
        (  # an orphan key, twice in the facts: one key
            {"sales": [["4", "99", "2026-01-07", "1.00"], ["5", "99", "2026-01-08", "1.00"]]},
            {("sale_customer", "referential_integrity"): {"orphans": 1}},
        ),
        (  # a repeated `to` key of a one-to-one
            {"profiles": [["10", "bronze"]]},
            {("customer_profile", "cardinality"): {"repeated": 1}},
        ),
    ],
)
def test_each_corruption_is_violated_with_its_exact_count(
    more: dict[str, list[list[str]]], violated: dict[tuple[str, str], dict[str, int]]
) -> None:
    expected = dict(VERIFIED) | {key: ("violated", counts) for key, counts in violated.items()}
    assert outcomes(with_rows(**more)) == expected


def test_overlap_counts_pairs_and_touching_intervals_never_overlap() -> None:
    """Closed-open: one interval ending where the next starts is no overlap; three versions
    over one instant are three pairs. A repeated key not referenced breaks no relationship."""
    history = [
        ["12", "a", "2026-01-01", "2026-03-01"],
        ["12", "b", "2026-02-01", "2026-03-01"],
        ["12", "c", "2026-02-15", "2026-04-01"],
        ["13", "a", "2026-01-01", "2026-02-01"],
        ["13", "b", "2026-02-01", "2026-03-01"],
    ]
    found = outcomes(with_rows(customer_history=history))
    assert found[("customer_history", "non_overlap")] == ("violated", {"overlaps": 3})
    assert found[("customer_at_sale", "cardinality")] == ("verified", {"overlaps": 0})


def test_a_one_to_one_is_checked_both_ways() -> None:
    found = outcomes(with_rows(customers=[["10", "West"]]))
    assert found[("customer_profile", "cardinality")] == ("violated", {"repeated": 1})


# --- rollup consistency: missing, additional and different groups -----------------------------


def rollup_outcome(change: Any) -> tuple[str, dict[str, int]]:  # noqa: ANN401
    data = rollups.lake_rows(rollups.SAMPLE)
    data["rollup"] = change([dict(r) for r in data["rollup"]])
    table = {
        name: [[r.get(c) or "" for c in rollups.CATALOG[name]] for r in rows]
        for name, rows in data.items()
    }
    with lake_of(table, rollups.CATALOG) as lake:
        found = verify(rollups.COMPILED, lake.read, lake.current_snapshot().id, at=AT)
    (record,) = [r for r in found.records if r.kind == "consistent"]
    return record.status, dict(record.violations)


def off_by_a_cent(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    rows[0]["revenue"] = f"{float(rows[0]['revenue'] or 0) + 0.01:.2f}"
    return rows


def null_revenue(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    rows[1]["revenue"] = None
    return rows


def additional(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    return [*rows, {"month": "2026-03-01", "category": "C", "revenue": "1.00", "quantity": "1"}]


@pytest.mark.parametrize(
    ("change", "violations"),
    [
        (lambda rows: rows, {"missing": 0, "additional": 0, "different": 0}),
        (lambda rows: rows[1:], {"missing": 1, "additional": 0, "different": 0}),
        (additional, {"missing": 0, "additional": 1, "different": 0}),
        (off_by_a_cent, {"missing": 0, "additional": 0, "different": 1}),
        (null_revenue, {"missing": 0, "additional": 0, "different": 1}),
        (lambda rows: additional(off_by_a_cent(rows[1:])), {"missing": 1, "additional": 1}),
    ],
)
def test_a_rollup_is_consistent_only_with_every_group_and_value(
    change: Any,  # noqa: ANN401
    violations: dict[str, int],
) -> None:
    status, found = rollup_outcome(change)
    assert {k: found[k] for k in violations} == violations
    assert status == ("verified" if not any(found.values()) else "violated")


def test_an_inconsistent_rollup_is_never_read_and_a_consistent_one_is() -> None:
    """P2-25's substitution reads the table exactly when `consistent` is verified on the
    snapshot read; otherwise the base, with the same answer."""
    document = rollups.query("revenue")
    for change, table_read in ((lambda rows: rows, "rollup"), (off_by_a_cent, None)):
        data = rollups.lake_rows(rollups.SAMPLE)
        data["rollup"] = change([dict(r) for r in data["rollup"]])
        table = {
            name: [[r.get(c) or "" for c in rollups.CATALOG[name]] for r in rows]
            for name, rows in data.items()
        }
        with lake_of(table, rollups.CATALOG) as lake:
            records = records_of(rollups.COMPILED, lake)
            found = ask(document, rollups.COMPILED, lake, evidence=records)
            base = ask(document, rollups.COMPILED, lake)  # nothing verified: the base
        assert found.receipt.document()["execution"].get("preaggregation") == table_read
        assert base.receipt.document()["execution"].get("preaggregation") is None
        assert ce.rows_of(found) == ce.rows_of(base), table_read


# --- bound to the snapshot and to the declaration ---------------------------------------------


def test_a_record_holds_for_its_snapshot_and_its_declaration_only() -> None:
    (unique,) = [
        d for d in declarations(COMPILED) if (d.dataset, d.kind) == ("customers", "unique")
    ]
    key = unique.key
    record = EvidenceRecord(*key, 7, unique.fingerprint, "violated", {"duplicates": 2}, AT)
    with pytest.raises(SemanticError):
        Ledger(COMPILED, [record], 7).uses([key])
    assert [u.status for u in Ledger(COMPILED, [record], 8).uses([key])] == ["declared"]
    assert [u.status for u in Ledger(COMPILED, [record], 6).uses([key])] == ["declared"]
    changed = compiled_of(document(customers={"evidence": ["unique"], "entity": "customer"}))
    assert [u.status for u in Ledger(changed, [record], 7).uses([key])] == ["declared"]
    other = compiled_of(document(sales={"evidence": ["unique"]}))  # another dataset changed
    with pytest.raises(SemanticError):
        Ledger(other, [record], 7).uses([key])


def test_verified_on_one_snapshot_is_declared_on_the_next() -> None:
    with lake_of(GOOD) as lake:
        first = lake.current_snapshot().id
        records = verify(COMPILED, lake.read, first, at=AT).records
        ledger = Ledger(COMPILED, records, first + 1)
    assert all(not ledger.verified(r.key) for r in records)
    assert all(Ledger(COMPILED, records, first).verified(r.key) for r in records)


# --- what a question relies on ----------------------------------------------------------------


def plan_of(found: Document, compiled: CompiledModel = COMPILED) -> Any:  # noqa: ANN401
    bound = bind_query(
        compiled,
        json.dumps(found),
        instant=ce.INSTANT,
        limits=Limits(detail_page_size=1000),
        hidden=lambda member: False,
    )
    return plan(analyze(bound, compiled, no_policies(compiled), ce.viewer_context()))


def relied_by(found: Document) -> list[tuple[str, str, str]]:
    return [(e, n.lower(), k) for e, n, k in relied(plan_of(found))]


def test_dependence_comes_from_the_plan_s_steps() -> None:
    assert relied_by(query("sales.sale_date")) == []
    assert relied_by(query("customers.region")) == [
        ("relationship", "sale_customer", "cardinality"),
    ]
    assert relied_by(query("customer_history.segment")) == [
        ("relationship", "customer_at_sale", "cardinality"),
    ]
    detail = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "sales.sale_id"}, {"member": "sales.amount"}],
        "order": [{"by": "sales.amount", "direction": "desc"}],
        "page": {"size": "10"},
    }
    assert relied_by(detail) == [("dataset", "sales", "unique")]


def test_violated_evidence_refuses_a_question_relying_on_it_before_any_read() -> None:
    with lake_of(with_rows(customers=[["10", "West"]])) as lake:
        records = records_of(COMPILED, lake)
        spy = Spy()
        with pytest.raises(SemanticError) as refused:
            ask(query("customers.region"), COMPILED, lake, evidence=records, read=spy)
        assert spy.calls == 0
        found = ask(query("sales.sale_date"), COMPILED, lake, evidence=records)  # not relying
    assert ce.rows_of(found) == [
        ["2026-01-05", "10.00"],
        ["2026-01-06", "5.00"],
        ["2026-02-01", "2.50"],
    ]
    error = refused.value
    assert error.code == "EVIDENCE_VIOLATED"
    builder = json.loads(error.to_json("en", "builder"))["details"]
    assert builder == {
        "dataset": "customers",
        "evidence": "cardinality",
        "fields": ["customer_id"],
        "violations": "1",
    }
    assert json.loads(error.to_json("en", "other"))["details"] == {
        "dataset": "customers",
        "evidence": "cardinality",
    }
    for audience in ("builder", "other"):  # no key value in any message
        assert "West" not in error.to_json("en", audience)
        assert '"10"' not in error.to_json("en", audience)


@pytest.mark.parametrize(
    ("more", "rows"),
    [
        (  # an orphan key: its row in the `NULL` group, exactly as without any record
            {"sales": [["4", "99", "2026-01-07", "1.00"]]},
            [["North", "12.50"], ["South", "5.00"], [None, "1.00"]],
        ),
        (  # a repeated key no sale refers to: nothing is multiplied
            {"customers": [["12", "West"], ["12", "East"]]},
            [["North", "12.50"], ["South", "5.00"]],
        ),
    ],
)
def test_violated_evidence_the_answer_does_not_depend_on_never_refuses(
    more: dict[str, list[list[str]]], rows: list[list[str | None]]
) -> None:
    """`referential_integrity` and the joined dataset's `unique` are violated and recorded,
    yet the answer is the same: they never refuse the question."""
    with lake_of(with_rows(**more)) as lake:
        records = records_of(COMPILED, lake)
        violated_kinds = {r.kind for r in records(lake_snapshot(lake)) if r.status == "violated"}
        found = ask(query("customers.region"), COMPILED, lake, evidence=records)
        unrecorded = ask(query("customers.region"), COMPILED, lake)
    assert violated_kinds & {"referential_integrity", "unique"}
    assert ce.rows_of(found) == ce.rows_of(unrecorded) == rows
    assert found.receipt.document()["evidence"][0]["status"] == "verified"


def lake_snapshot(lake: Lake) -> dict[str, int]:
    return {"id": lake.current_snapshot().id}


def test_the_receipt_lists_declared_and_verified_evidence_exactly() -> None:
    with lake_of(GOOD) as lake:
        declared = ask(query("customers.region"), COMPILED, lake)
        verified = ask(
            query("customers.region"), COMPILED, lake, evidence=records_of(COMPILED, lake)
        )
    assert declared.receipt.document()["evidence"] == [
        {"dataset": "customers", "kind": "cardinality", "status": "declared"},
    ]
    at = "2026-11-06T12:00:00.000000Z"
    assert sorted(verified.receipt.document()["evidence"], key=str) == [
        {
            "dataset": d,
            "kind": k,
            "status": "verified",
            "verified_by_run": "run-1",
            "verified_at": at,
        }
        for d, k in (("customers", "cardinality"),)
    ]
    assert ce.rows_of(declared) == ce.rows_of(verified) == [["North", "12.50"], ["South", "5.00"]]


# --- the budget -------------------------------------------------------------------------------


def test_a_check_over_its_budget_stays_declared() -> None:
    def timed_out(request: Any) -> Any:  # noqa: ANN401
        raise StoreError("STORE_TIMEOUT", "the read took too long", {})

    found = verify(COMPILED, timed_out, 1, at=AT, budget_ms=1000)
    assert found.records == ()
    assert {s.reason for s in found.skipped} == {"budget", "first"}
    assert {s.declaration.kind for s in found.skipped if s.reason == "first"} == {"append_only"}
    spy = Spy()
    nothing_left = verify(COMPILED, spy, 1, at=AT, budget_ms=0)  # nothing left: nothing read
    assert spy.calls == 0
    assert nothing_left.records == ()


def test_any_other_failure_of_a_check_is_raised() -> None:
    def broken(request: Any) -> Any:  # noqa: ANN401
        raise StoreError("STORE_QUERY_FAILED", "broken", {"duckdb": "Binder Error"})

    with pytest.raises(StoreError):
        verify(COMPILED, broken, 1, at=AT)


def test_a_record_names_its_run_instant_and_declaration() -> None:
    with lake_of(GOOD) as lake:
        snapshot = lake.current_snapshot().id
        found = verify(COMPILED, lake.read, snapshot, at=AT, run="run-9", only=["customers"])
    fingerprints = {d.key: d.fingerprint for d in declarations(COMPILED)}
    assert [(r.key, r.snapshot, r.run, r.at) for r in found.records] == [
        (("dataset", "CUSTOMERS", "unique"), snapshot, "run-9", AT)
    ]
    assert found.records[0].declaration == fingerprints[found.records[0].key]
    assert replace(found.records[0], snapshot=snapshot).status == "verified"


# --- append_only against the dataset's previous verified snapshot (#355) -----------------------


def statement(lake: Lake, *sql: str) -> int:
    with lake.transaction() as transaction:
        for text in sql:
            transaction.execute(StatementRequest(kind="statement", sql=text, parameters=[]))
    return lake.current_snapshot().id


# A data file, not rows kept in the catalog.
MORE_SALES = "INSERT INTO sales SELECT 100 + range, 10, DATE '2026-03-01', 1.00 FROM range(20)"


@pytest.mark.parametrize(
    ("changes", "violations"),
    [
        ((MORE_SALES,), {"removed": 0}),  # rows only added
        (("UPDATE sales SET amount = 9.99 WHERE sale_id = 1",), {"removed": 1}),
        (("DELETE FROM sales WHERE sale_id = 2",), {"removed": 1}),
        (
            (
                "UPDATE sales SET amount = 9.99 WHERE sale_id = 1",
                "DELETE FROM sales WHERE sale_id = 2",
            ),
            {"removed": 2},
        ),
        (  # rows added after the baseline, then changed: the baseline's rows all remain
            (
                MORE_SALES,
                "DELETE FROM sales WHERE sale_id = 101",
                "UPDATE sales SET amount = 0.50 WHERE sale_id = 102",
            ),
            {"removed": 0},
        ),
        (  # a row deleted and written back unchanged: compared by value, it remains
            (
                "DELETE FROM sales WHERE sale_id = 3",
                "INSERT INTO sales VALUES (3, 10, DATE '2026-02-01', 2.50)",
            ),
            {"removed": 0},
        ),
        (  # an update to the same value
            ("UPDATE sales SET amount = 10.00 WHERE sale_id = 1",),
            {"removed": 0},
        ),
        (  # rebuilt without one of its rows
            ("CREATE OR REPLACE TABLE sales AS SELECT * FROM sales WHERE sale_id <> 2",),
            {"removed": 1},
        ),
    ],
)
def test_append_only_counts_every_row_of_the_baseline_changed_or_removed(
    changes: tuple[str, ...], violations: dict[str, int]
) -> None:
    with lake_of(GOOD) as lake:
        baseline = lake.current_snapshot().id
        first = verify(COMPILED, lake.read, baseline, at=AT, only=["sales"], changes=lake.changes)
        assert [s.reason for s in first.skipped if s.declaration.kind == "append_only"] == ["first"]
        for text in changes:  # one snapshot each: the comparison spans all of them
            statement(lake, text)
        found = verify(
            COMPILED,
            lake.read,
            lake.current_snapshot().id,
            at=AT,
            only=["sales"],
            changes=lake.changes,
            baselines={"SALES": baseline},
        )
    (record,) = [r for r in found.records if r.kind == "append_only"]
    assert dict(record.violations) == violations
    assert record.status == ("violated" if any(violations.values()) else "verified")
    assert record.tables == ("sales",)


def test_append_only_counts_rows_of_data_files_in_the_baseline() -> None:
    with lake_of(GOOD) as lake:
        baseline = statement(lake, MORE_SALES)
        statement(
            lake,
            "DELETE FROM sales WHERE sale_id = 101",
            "UPDATE sales SET amount = 0.50 WHERE sale_id = 102",
        )
        found = verify(
            COMPILED,
            lake.read,
            lake.current_snapshot().id,
            at=AT,
            only=["sales"],
            changes=lake.changes,
            baselines={"SALES": baseline},
        )
    (record,) = [r for r in found.records if r.kind == "append_only"]
    assert dict(record.violations) == {"removed": 2}


def test_a_record_holds_later_only_while_no_table_it_read_changed() -> None:
    """Records of an earlier snapshot hold when the store proves the tables their check read
    unchanged since (another table written, say); never once one of them changed."""
    with lake_of(GOOD) as lake:
        checked = lake.current_snapshot().id
        records = verify(COMPILED, lake.read, checked, at=AT).records

        def unchanged(record: EvidenceRecord, snapshot: int) -> bool:
            request = {
                "kind": "changes",
                "tables": list(record.tables),
                "since": record.snapshot,
                "snapshot": snapshot,
            }
            found = lake.changes(ChangesRequest.model_validate(request))
            return not any(t.changed for t in found.tables)

        other = statement(lake, "CREATE SCHEMA _meta", "CREATE TABLE _meta.notes (x INTEGER)")
        held = Ledger(COMPILED, records, other, unchanged)
        assert {r.key for r in records} == set(held.records)
        written = statement(lake, "INSERT INTO profiles VALUES (12, 'gold')")
        after = Ledger(COMPILED, records, written, unchanged)
        # Only the check that read `profiles` stops holding.
        assert set(held.records) - set(after.records) == {
            ("relationship", "CUSTOMER_PROFILE", "cardinality")
        }
        assert Ledger(COMPILED, records, other).records == {}  # no proof: nothing earlier holds
