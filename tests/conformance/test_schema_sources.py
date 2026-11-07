"""#359: a dataset's source in a lake schema (`marts.x`) is read as two identifiers, in every
read; a source of any other shape never reaches SQL."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from orvex.contracts.store.v1 import StatisticsRequest, StatisticsResult
from orvex.semantic.errors import SemanticError
from orvex.semantic.evidence import verify
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners.semantic_corpus import CORPUS, ROOT
from tests.conformance import test_materialization as rollups
from tests.conformance import test_row_policies as rp
from tests.conformance.test_execution import ask

AT = datetime(2026, 11, 7, tzinfo=UTC)
CASES = json.loads((ROOT / CORPUS / "corpus" / "manifest.json").read_text("utf-8"))["cases"]


def outcome(case: str, schema: str | None) -> Any:  # noqa: ANN401
    folder = ROOT / CORPUS / "corpus" / "cases" / case
    model = ROOT / CORPUS / "models" / f"{case}.yaml"
    error = folder / "expected" / "error.json"
    code = None
    if error.exists():
        from conformance.runners import bi_platform_adapter as adapter

        code = adapter.product_error_code(json.loads(error.read_text("utf-8"))["code"])
    return ce.run_case(case, folder, model, code, schema)


@pytest.mark.parametrize("case", [c["id"] for c in CASES])
def test_every_corpus_case_answers_the_same_from_a_lake_schema(case: str) -> None:
    """Joins, roles, fan-out, multi-fact, bridges, as-of joins, cumulative metrics and row
    policies: each case's outcome with every table in `marts` is its outcome in `main`."""
    assert outcome(case, "marts") == outcome(case, None)


@pytest.mark.parametrize("source", ["a.b.c", "marts.", ".sales", "", " sales", "marts. sales"])
def test_a_source_of_any_other_shape_is_refused_when_the_model_compiles(source: str) -> None:
    model = ROOT / CORPUS / "models" / "CASE-001.yaml"
    from orvex.semantic.compiler.compiled import compile_model
    from orvex.semantic.ossie.reader import read

    from conformance.runners import bi_platform_adapter as adapter

    translated, _ = adapter.translate(cast(dict[str, Any], adapter.load_yaml(model)), ce.VENDORS[0])
    translated["datasets"][0]["source"] = source

    def compiled() -> object:
        loaded = read(json.dumps(translated).encode(), "semantic/m.ossie.json", ce.VENDORS)
        return compile_model(loaded, ce.catalog_of(model), ce.VENDORS)

    with pytest.raises(SemanticError) as refused:
        compiled()
    listed = cast(list[dict[str, Any]], refused.value.details["problems"])
    problems = [{k: p[k] for k in ("pointer", "rule")} for p in listed]
    # An empty source is no Ossie name (`schema`); any other shape, no table (`reference`).
    rule = "schema" if source == "" else "reference"
    assert {"pointer": "/datasets/0/source", "rule": rule} in problems


@contextmanager
def sales_lake(compiled: Any, schema: str | None) -> Iterator[Any]:  # noqa: ANN401
    """The rollup tests' sales and their monthly rollup, in `main` or in `schema`."""
    data = rollups.lake_rows(rollups.SAMPLE)
    with TemporaryDirectory() as folder:
        paths = []
        for name in ("sales", "rollup"):
            path = Path(folder) / f"{name}.csv"
            columns = list(rollups.CATALOG[name])
            lines = [",".join(columns)] + [
                ",".join(r.get(c) or "" for c in columns) for r in data[name]
            ]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        catalog = {
            (n if schema is None else f"{schema}.{n}"): rollups.CATALOG[n]
            for n in ("sales", "rollup")
        }
        with ce.case_lake(paths, catalog, schema) as lake:
            yield lake


def statistics_of(lake: Lake) -> Any:  # noqa: ANN401
    def statistics(request: StatisticsRequest) -> StatisticsResult | None:
        try:
            return lake.statistics(request)
        except StoreError:
            return None

    return statistics


DETAIL = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "detail",
    "fields": [{"member": "sales.sale_id"}, {"member": "sales.category"}],
    "order": [{"by": "sales.sale_id", "direction": "asc"}],
    "page": {"size": "100"},
}


def answers(schema: str | None) -> dict[str, Any]:
    compiled = rollups.model(rollups.MONTHLY, schema=schema)
    with sales_lake(compiled, schema) as lake:
        evidence = rollups.checked(compiled, lake)
        aggregate = ask(rollups.query("revenue"), compiled, lake, evidence=evidence)
        detail = ask(DETAIL, compiled, lake, statistics=statistics_of(lake))
        records = verify(compiled, lake.read, lake.current_snapshot().id, at=AT).records
    return {
        "aggregate": ce.rows_of(aggregate),
        "rollup": aggregate.receipt.document()["execution"].get("preaggregation"),
        "detail": ce.rows_of(detail),
        "estimate": detail.receipt.document()["execution"].get("estimate"),
        "evidence": sorted((r.name, r.kind, r.status, dict(r.violations)) for r in records),
    }


def test_rollups_evidence_detail_pages_and_estimates_read_a_lake_schema() -> None:
    """The aggregate table read in place of its base, its consistency check, a detail page
    and its read-cost estimate: each the same from `marts` as from `main`."""
    placed, plain = answers("marts"), answers(None)
    assert placed == plain
    assert placed["rollup"] == "rollup"
    assert placed["estimate"] is not None
    assert placed["evidence"] == [
        ("ROLLUP", "consistent", "verified", {"missing": 0, "additional": 0, "different": 0})
    ]


COUNTRY = {
    "name": "own_country",
    "dataset": "customers",
    "kind": "predicate",
    "predicate": "country = $country",
}
ZONE = {
    "name": "own_zone",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)",
}
THROUGH_JOIN = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "detail",
    "fields": [{"member": "sales.sale_id"}, {"member": "customers.country"}],
    "order": [{"by": "sales.sale_id", "direction": "asc"}],
    "page": {"size": "100"},
}


def guarded(schema: str | None) -> list[Any]:
    compiled = rp.model(schema)
    found = []
    with rp.lake_of(schema=schema) as lake:
        for rule, context, document in (
            (rp.TENANT, rp.caller(tenant="A"), rp.query()),  # a policy on the fact
            (COUNTRY, rp.caller(country="BO"), rp.query()),  # propagated from a dimension
            (COUNTRY | {"orphans": "visible"}, rp.caller(country="BO"), rp.query()),
            (ZONE, rp.caller(user_id="u-1"), rp.query()),  # a mapping table
            (COUNTRY, rp.caller(country="BO"), THROUGH_JOIN),  # a detail page through a join
        ):
            policy = rp.policies(rule, compiled=compiled)
            answered = ask(document, compiled, lake, policy=policy, context=context)
            found.append(ce.rows_of(answered))
    return found


def test_row_policies_and_detail_pages_through_joins_read_a_lake_schema() -> None:
    """A policy on the fact, one propagated from a dimension (orphans hidden and visible), a
    mapping table, and a detail page through a join under a policy: the same from `marts`."""
    placed = guarded("marts")
    assert placed == guarded(None)
    assert placed[1] == [["North", "1099.00"]]  # propagated: sale 4 has no customer
    assert placed[2] == [["North", "1106.00"]]  # orphans visible
    assert placed[3] == [["North", "1106.00"]]  # zone 1 only
    assert placed[-1] == [["1", "BO"], ["3", "BO"]]


def test_a_restricted_schema_table_read_any_other_way_is_refused() -> None:
    """From `marts`: the restricted relation is accepted; the same table read bare, through
    the catalog, in another case or fully qualified is refused before any read."""
    from orvex.semantic.adapters.duckdb import compile_plan
    from orvex.semantic.correctness import analyze
    from orvex.semantic.correctness.discharge import DischargeError, verify_physical
    from orvex.semantic.planning import plan
    from orvex.semantic.query.binding import Limits, bind_query

    compiled = rp.model("marts")
    policy = rp.policies(rp.TENANT, compiled=compiled)
    bound = bind_query(
        compiled,
        json.dumps(rp.query()),
        instant=ce.INSTANT,
        limits=Limits(detail_page_size=1000),
        hidden=lambda member: False,
    )
    found = plan(analyze(bound, compiled, policy, rp.caller(tenant="A")))
    written = compile_plan(found)
    assert '(SELECT * FROM "marts"."sales" AS "#p" WHERE ' in written.sql
    verify_physical(found, written)
    for read in ('"marts"."sales"', '"lake"."sales"', '"MARTS"."Sales"', '"lake"."marts"."sales"'):
        broken = replace(written, sql=f'{written.sql} UNION ALL SELECT * FROM {read} AS "x"')
        with pytest.raises(DischargeError):
            verify_physical(found, broken)
