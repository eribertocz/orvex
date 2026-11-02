"""P2-05b (#283): binding a `semantic-query` v1 text to its canonical form and fingerprint."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.compiler.catalog import SourceCatalog, SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read
from orvex.semantic.query.binding import BoundQuery, Limits, bind_query, relative_range

from conformance.runners import bi_platform_adapter as adapter

ROOT = Path(__file__).resolve().parents[3]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])
CORPUS = ROOT / "conformance/semantic-corpus/bi-platform/models"
QUERIES = json.loads((ROOT / "conformance/runners/corpus_queries.json").read_text("utf-8"))
GOLDEN = Path(__file__).with_name("golden") / "corpus-query-fingerprints.json"
NOT_YET = {"CASE-006", "CASE-009", "CASE-021"}  # their models compile after P2-13/P2-17/P2-20
INSTANT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
LIMITS = Limits(detail_page_size=1000)

Model = dict[str, Any]

CATALOG: SourceCatalog = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "region": SourceColumn("VARCHAR"),
        "amount": SourceColumn("DECIMAL(18,2)"),
        "sold_on": SourceColumn("DATE", False),
        "sold_at": SourceColumn("TIMESTAMP WITH TIME ZONE"),
        "customer_id": SourceColumn("BIGINT", False),
    },
    "customers": {"customer_id": SourceColumn("BIGINT", False), "segment": SourceColumn("VARCHAR")},
}


def extension(payload: dict[str, Any]) -> Model:
    return {"vendor_name": CURRENT, "data": json.dumps({"version": 1, **payload})}


def field(name: str, **payload: Any) -> Model:  # noqa: ANN401
    node: Model = {"name": name, "expression": {"sql": name}}
    if payload:
        node["custom_extensions"] = [extension({"element": "field", **payload})]
    return node


def model(*, zone: str | None = "America/La_Paz", parameters: list[Model] | None = None) -> Model:
    payload: dict[str, Any] = {"element": "model"}
    if zone is not None:
        payload["default_time_zone"] = zone
    if parameters:
        payload["parameters"] = parameters
    return {
        "version": "0.2.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [
                    field("sale_id"),
                    field("region"),
                    field("amount"),
                    field("sold_on"),
                    field("sold_at"),
                    field("customer_id"),
                ],
                "metrics": [{"name": "order_count", "expression": {"sql": "COUNT(sale_id)"}}],
            },
            {
                "name": "customers",
                "source": "customers",
                "primary_key": ["customer_id"],
                "fields": [field("customer_id"), field("segment")],
            },
        ],
        "relationships": [
            {
                "name": "customer",
                "from": "sales",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}},
            {"name": "buyers", "expression": {"sql": "COUNT(sales.customer_id)"}},
        ],
        "custom_extensions": [extension(payload)],
    }


def compiled(document: Model | None = None) -> CompiledModel:
    loaded = read(json.dumps(document or model()).encode(), "semantic/shop.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


def query(**more: Any) -> Model:  # noqa: ANN401
    found: Model = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
        "group_by": [{"member": "sales.region"}],
    }
    found.update(more)
    return found


def bind(document: Model | str, target: CompiledModel | None = None, **options: Any) -> BoundQuery:  # noqa: ANN401
    text = document if isinstance(document, str) else json.dumps(document)
    return bind_query(
        target or compiled(),
        text,
        instant=options.get("instant", INSTANT),
        limits=options.get("limits", LIMITS),
    )


def failure(document: Model | str, target: CompiledModel | None = None) -> SemanticError:
    with pytest.raises(SemanticError) as caught:
        bind(document, target)
    return caught.value


def problems(error: SemanticError) -> list[tuple[str, str]]:
    found = cast(list[dict[str, str]], error.details["problems"])
    return [(p["rule"], p["pointer"]) for p in found]


def text(value: str) -> Model:
    return {"type": "text", "value": value}


def visual(member: str, predicate: Model, scope: str = "visual") -> Model:
    return {"scope": scope, "member": member, "predicate": predicate}


# --- the case matrix ---------------------------------------------------------------------------


def test_a_plain_query_binds() -> None:
    bound = bind(query())
    assert bound.fingerprint.startswith("sha256:")
    assert bound.canonical["metrics"][0][0] == ("metric", "", "REVENUE")


def test_a_number_as_a_value_is_a_schema_problem() -> None:
    document = query(
        filters=[visual("sales.region", {"op": "in", "values": [{"type": "integer", "value": 10}]})]
    )
    error = failure(document)
    assert error.code == "QUERY_INVALID"
    assert {rule for rule, _ in problems(error)} == {"schema"}


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind": "semantic_query", "kind": "semantic_query", "version": 1}',
        '{"kind": "semantic_query", "version": NaN}',
        "not json",
        "[1, 2]",
    ],
)
def test_text_that_is_not_strict_json_is_a_syntax_problem(raw: str) -> None:
    error = failure(raw)
    assert error.code == "QUERY_INVALID"
    assert problems(error) in ([("syntax", "")], [("schema", "")])


def test_an_unknown_member_names_only_what_the_caller_wrote() -> None:
    error = failure(query(group_by=[{"member": "sales.nope"}]))
    assert (error.code, dict(error.details)) == ("MEMBER_NOT_FOUND", {"member": "sales.nope"})
    error = failure(query(group_by=[{"member": "sales.region", "role": "billing"}]))
    assert (error.code, dict(error.details)) == ("MEMBER_NOT_FOUND", {"member": "billing"})


@pytest.mark.parametrize(
    ("document", "pointer"),
    [
        (query(metrics=[{"metric": "sales.amount"}]), "/metrics/0/metric"),
        (query(group_by=[{"member": "revenue"}]), "/group_by/0/member"),
        (
            query(group_by=[{"member": "sales.region", "bucket": {"unit": "month"}}]),
            "/group_by/0/member",
        ),
        (
            query(group_by=[{"member": "sales.sold_on", "bucket": {"unit": "hour"}}]),
            "/group_by/0/bucket/unit",
        ),
        (query(metrics=[{"metric": "revenue", "context": "nope"}]), "/metrics/0/context"),
        (query(metrics=[{"metric": "revenue", "apply": ["ytd"]}]), "/metrics/0/apply/0"),
        (
            query(filters=[visual("sales.region", {"op": "in_segment", "segment": "vip"})]),
            "/filters/0/predicate/segment",
        ),
    ],
)
def test_a_member_of_the_wrong_kind_is_a_kind_problem(document: Model, pointer: str) -> None:
    error = failure(document)
    assert error.code == "QUERY_INVALID"
    assert problems(error) == [("kind", pointer)]


def test_a_relative_date_with_no_zone_anywhere_is_a_zone_problem() -> None:
    predicate = {"op": "relative_date", "unit": "month", "offset": "-1", "count": "1"}
    error = failure(query(filters=[visual("sales.sold_on", predicate)]), compiled(model(zone=None)))
    assert problems(error) == [("zone", "/filters/0/predicate")]


def test_parameters_are_checked_against_their_declaration() -> None:
    declared = [{"name": "n", "type": "number", "default": "5", "allowed": ["5", "10"]}]
    target = compiled(model(parameters=declared))
    for value in (text("abc"), {"type": "decimal", "value": "7"}):
        error = failure(query(parameters=[{"name": "n", "value": value}]), target)
        assert (error.code, dict(error.details)) == (
            "PARAMETER_INVALID",
            {"parameter": "n", "expected": "number"},
        )
    bound = bind(
        query(parameters=[{"name": "n", "value": {"type": "integer", "value": "10"}}]), target
    )
    assert bound.canonical["parameters"] == (("N", "number", "10"),)
    assert bind(query(), target).canonical["parameters"] == (("N", "number", "5"),)
    error = failure(query(parameters=[{"name": "m", "value": text("x")}]), target)
    assert (error.code, dict(error.details)) == ("MEMBER_NOT_FOUND", {"member": "m"})


def test_as_known_at_needs_history_on_every_dataset_read() -> None:
    error = failure(query(as_known_at="2026-10-01T00:00:00Z"))
    assert (error.code, dict(error.details)) == ("AS_KNOWN_AT_UNSUPPORTED", {"dataset": "sales"})


def test_two_metric_filters_on_one_dimension_and_scope_are_a_cycle() -> None:
    def metric_filter(metric: str) -> Model:
        return {
            "scope": "visual",
            "metric": metric,
            "dimension": "sales.customer_id",
            "condition": {"op": "top", "n": "10"},
        }

    error = failure(query(metric_filters=[metric_filter("revenue"), metric_filter("buyers")]))
    assert (error.code, dict(error.details)) == ("FILTER_CYCLE", {"filters": ["revenue", "buyers"]})


def detail(**more: Any) -> Model:  # noqa: ANN401
    found: Model = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "sales.amount"}],
        "order": [{"by": "sales.region"}],
        "page": {"size": "50"},
    }
    found.update(more)
    return found


def test_a_detail_page_above_the_cap_exceeds_the_budget() -> None:
    error = failure(detail(page={"size": "5000"}))
    assert (error.code, dict(error.details)) == (
        "QUERY_BUDGET_EXCEEDED",
        {"budget": "result_rows", "limit": "1000"},
    )
    bound = bind(detail())
    assert bound.canonical["tie_breaker"] == (("field", "SALES", "SALE_ID"),)


def test_not_in_keeps_nulls_unless_excluded() -> None:
    bound = bind(query(filters=[visual("sales.region", {"op": "not_in", "values": [text("A")]})]))
    predicate = bound.canonical["filters"][0]["predicate"]
    assert predicate["exclude_null"] is False  # NULLs are kept (#238 rule 6)
    excluded = bind(
        query(
            filters=[
                visual(
                    "sales.region", {"op": "not_in", "values": [text("A")], "exclude_null": True}
                )
            ]
        )
    )
    assert excluded.fingerprint != bound.fingerprint


def test_filters_in_several_scopes_stay_apart() -> None:
    page = visual("sales.region", {"op": "in", "values": [text("A"), text("B")]}, "page")
    inner = visual("sales.region", {"op": "in", "values": [text("B")]})
    filters = bind(query(filters=[inner, page])).canonical["filters"]
    assert [f["scope"] for f in filters] == [1, 2]  # page, then visual: both kept


def test_equal_and_different_queries() -> None:
    def with_values(*values: str) -> Model:
        return query(
            filters=[visual("sales.region", {"op": "in", "values": [text(v) for v in values]})]
        )

    assert bind(with_values("b", "a", "a")).fingerprint == bind(with_values("a", "b")).fingerprint
    assert bind(query(metrics=[{"metric": "REVENUE"}])).fingerprint == bind(query()).fingerprint
    two = query(metrics=[{"metric": "revenue"}, {"metric": "buyers"}])
    reversed_ = query(metrics=[{"metric": "buyers"}, {"metric": "revenue"}])
    assert bind(two).fingerprint != bind(reversed_).fingerprint


def test_top_n_records_its_tie_rule() -> None:
    bound = bind(query(top_n={"by": "revenue", "n": "3", "others": True}))
    assert bound.canonical["top_n"]["ties"] == "member_value_ascending"


# --- values and relative dates -----------------------------------------------------------------


def test_values_are_normalized_exactly() -> None:
    def ranged(low: Model) -> BoundQuery:
        return bind(query(filters=[visual("sales.amount", {"op": "range", "gte": low})]))

    assert (
        ranged({"type": "decimal", "value": "1.50"}).fingerprint
        == ranged({"type": "decimal", "value": "1.5"}).fingerprint
    )
    assert (
        ranged({"type": "decimal", "value": "1.50"}).fingerprint
        != ranged({"type": "decimal", "value": "1.51"}).fingerprint
    )
    at = bind(
        query(
            filters=[
                visual(
                    "sales.sold_at",
                    {
                        "op": "range",
                        "lt": {"type": "timestamp", "value": "2026-01-01T00:00:00-04:00"},
                    },
                )
            ]
        )
    )
    utc = bind(
        query(
            filters=[
                visual(
                    "sales.sold_at",
                    {"op": "range", "lt": {"type": "timestamp", "value": "2026-01-01T04:00:00Z"}},
                )
            ]
        )
    )
    assert at.fingerprint == utc.fingerprint


@pytest.mark.parametrize(
    "value",
    [
        {"type": "date", "value": "2026-13-45"},
        {"type": "timestamp", "value": "2026-01-01T25:00:00Z"},
        {"type": "timestamp", "value": "2026-01-01T00:00:00+15:00"},
        {"type": "local_timestamp", "value": "2026-02-30T00:00:00"},
    ],
)
def test_values_that_do_not_exist_are_schema_problems(value: Model) -> None:
    error = failure(query(filters=[visual("sales.sold_on", {"op": "range", "gte": value})]))
    assert problems(error) == [("schema", "/filters/0/predicate/gte/value")]


def test_a_trailing_line_break_is_refused() -> None:
    error = failure(query(metrics=[{"metric": "revenue\n"}]))
    assert {rule for rule, _ in problems(error)} == {"schema"}


@pytest.mark.parametrize(
    ("unit", "offset", "count", "to_date", "expected"),
    [
        # Owner decision of 2026-11-01: count whole periods ending at period offset.
        ("month", 0, 1, False, ("2026-11-01T00:00:00", "2026-12-01T00:00:00")),
        ("month", -1, 3, False, ("2026-08-01T00:00:00", "2026-11-01T00:00:00")),
        ("year", -1, 1, True, ("2025-01-01T00:00:00", "2025-11-01T08:00:00")),
        ("week", 0, 1, False, ("2026-10-26T00:00:00", "2026-11-02T00:00:00")),  # ISO Monday
        ("day", -1, 7, False, ("2026-10-25T00:00:00", "2026-11-01T00:00:00")),
        ("quarter", 0, 1, True, ("2026-10-01T00:00:00", "2026-11-01T08:00:00")),
    ],
)
def test_relative_ranges(
    unit: str, offset: int, count: int, to_date: bool, expected: tuple[str, str]
) -> None:
    start, end = relative_range(INSTANT, "America/La_Paz", unit, offset, count, to_date)
    naive = (start.replace(tzinfo=None).isoformat(), end.replace(tzinfo=None).isoformat())
    assert naive == expected


def test_a_relative_date_binds_to_an_absolute_range_beside_the_original() -> None:
    predicate = {"op": "relative_date", "unit": "month", "offset": "-1", "count": "1"}
    bound = bind(query(filters=[visual("sales.sold_at", predicate)]))
    resolved = bound.canonical["filters"][0]["predicate"]
    assert resolved["op"] == "range"
    assert resolved["gte"] == ("timestamp", "2026-10-01T04:00:00Z")
    assert resolved["lt"] == ("timestamp", "2026-11-01T04:00:00Z")
    assert resolved["relative_date"]["unit"] == "month"
    on_dates = bind(query(filters=[visual("sales.sold_on", predicate)]))
    assert on_dates.canonical["filters"][0]["predicate"]["lt"] == ("date", "2026-11-01")


def test_a_relative_date_does_not_follow_the_host_zone() -> None:
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from packages.semantic.tests import test_query_binding as t; "
        "predicate = {'op': 'relative_date', 'unit': 'day', 'offset': '0', 'count': '1'}; "
        "print(t.bind(t.query(filters=[t.visual('sales.sold_at', predicate)])).fingerprint)"
    )
    prints = set()
    for zone in ("Pacific/Kiritimati", "America/Adak"):
        run = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env={**os.environ, "TZ": zone},
        )
        prints.add(run.stdout.strip())
    assert len(prints) == 1


def test_the_instant_must_carry_its_zone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        bind(query(), instant=datetime(2026, 11, 1, 12, 0))
    other = datetime(2026, 11, 1, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert bind(query(), instant=other).fingerprint == bind(query()).fingerprint


# --- the corpus --------------------------------------------------------------------------------


def corpus_model(path: Path) -> CompiledModel:
    translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
    written = yaml.safe_load(path.with_name(f"{path.stem}.catalog.yaml").read_text("utf-8"))
    catalog = {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }
    loaded = read(json.dumps(translated).encode(), f"semantic/{path.stem}.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS)


def corpus_fingerprints() -> dict[str, str]:
    found = {}
    for case, document in sorted(QUERIES["queries"].items()):
        if case in NOT_YET:
            continue
        found[case] = bind(document, corpus_model(CORPUS / f"{case}.yaml")).fingerprint
    return found


def test_every_corpus_query_is_written_and_binds() -> None:
    assert sorted(QUERIES["queries"]) == sorted(p.stem for p in CORPUS.glob("CASE-0??.yaml"))
    golden = json.loads(GOLDEN.read_text("utf-8"))
    assert corpus_fingerprints() == golden


@pytest.mark.parametrize("case", sorted(NOT_YET))
def test_queries_of_models_that_do_not_compile_yet_are_valid(case: str) -> None:
    from orvex.semantic.query.binding import _decode

    assert _decode(json.dumps(QUERIES["queries"][case]))["mode"] == "aggregate"


@pytest.mark.xfail(
    reason="CASE-006, -009 and -021 compile once P2-13, P2-17 and P2-20 supply their "
    "declarations (Owner decision of 2026-11-01, #283)",
    strict=True,
)
@pytest.mark.parametrize("case", sorted(NOT_YET))
def test_queries_of_models_that_do_not_compile_yet_bind(case: str) -> None:
    bind(QUERIES["queries"][case], corpus_model(CORPUS / f"{case}.yaml"))


def test_fingerprints_are_equal_across_processes_and_hash_seeds() -> None:
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from packages.semantic.tests import test_query_binding as t; "
        "print(json.dumps(t.corpus_fingerprints(), sort_keys=True))"
    )
    golden = json.loads(GOLDEN.read_text("utf-8"))
    for seed in ("0", "1", "4242"):
        run = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert json.loads(run.stdout) == golden


# --- properties --------------------------------------------------------------------------------


def rich() -> Model:
    return query(
        metrics=[{"metric": "revenue"}, {"metric": "buyers"}],
        group_by=[{"member": "sales.region"}, {"member": "customers.segment", "role": "customer"}],
        filters=[
            visual("sales.region", {"op": "in", "values": [text("North"), text("South")]}),
            visual("sales.region", {"op": "not_in", "values": [text("X")]}, "page"),
            visual("sales.amount", {"op": "range", "gte": {"type": "decimal", "value": "10"}}),
        ],
        order=[{"by": "revenue", "direction": "desc"}],
        context_modifiers=[
            {"op": "remove", "scope": "page", "members": ["sales.region", "customers.segment"]}
        ],
    )


def shuffled(value: object, seed: int) -> object:
    """The same query: keys reordered, set members permuted and duplicated, identifier case
    changed, defaults written out."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: hash((k, seed)))
        found: dict[str, Any] = {k: shuffled(value[k], seed + 1) for k in keys}
        if found.get("op") == "in":
            found.setdefault("include_null", False)
        if found.get("op") == "not_in":
            found.setdefault("exclude_null", False)
        if "by" in found and "direction" in found:
            found.setdefault("nulls", "last")
        for key in ("metric", "member", "by"):
            if isinstance(found.get(key), str) and '"' not in found[key]:
                found[key] = found[key].upper() if seed % 2 else found[key].lower()
        return found
    if isinstance(value, list):
        return [shuffled(item, seed + 1) for item in value]
    return value


def permute_sets(document: Model, seed: int) -> Model:
    found = copy.deepcopy(document)
    found["filters"] = found["filters"][seed % 3 :] + found["filters"][: seed % 3]
    values = found["filters"][0]["predicate"].get("values")
    if values:
        values.append(values[0])
        values.reverse()
    return found


@settings(max_examples=60, deadline=None)
@given(st.integers(0, 1000), st.booleans())
def test_equivalent_queries_have_equal_canonical_forms(seed: int, spaced: bool) -> None:
    original = bind(rich())
    variant = cast(Model, shuffled(permute_sets(rich(), seed), seed))
    raw = json.dumps(variant, indent=4 if spaced else None)
    bound = bind(raw)
    assert bound.canonical == original.canonical
    assert bound.fingerprint == original.fingerprint


MUTATIONS = {
    "value": lambda d: d["filters"][0]["predicate"]["values"].__setitem__(0, text("East")),
    "scope": lambda d: d["filters"][0].update(scope="board"),
    "predicate": lambda d: d["filters"][0].update(predicate={"op": "is_null"}),
    "member": lambda d: d["group_by"].__setitem__(0, {"member": "sales.amount"}),
    "direction": lambda d: d["order"][0].update(direction="asc"),
    "include_null": lambda d: d["filters"][0]["predicate"].update(include_null=True),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_a_semantic_mutation_changes_the_fingerprint(mutation: str) -> None:
    mutated = rich()
    MUTATIONS[mutation](mutated)
    assert bind(mutated).fingerprint != bind(rich()).fingerprint


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=200, deadline=None)
@given(
    st.one_of(
        _JSON,
        st.builds(
            lambda extra: {**rich(), **extra},
            st.dictionaries(
                st.sampled_from(
                    ["limit", "top_n", "coverage", "order", "filters", "parameters", "page"]
                ),
                _JSON,
                max_size=2,
            ),
        ),
    )
)
def test_any_input_gives_a_bound_query_or_a_semantic_error(document: object) -> None:
    with contextlib.suppress(SemanticError):
        bind(json.dumps(document))


def test_the_binder_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/semantic/src/orvex/semantic/query/semantic-query-v1.json"
    contract = ROOT / "contracts/semantic-query/v1/schema.json"
    assert packaged.read_bytes() == contract.read_bytes()


# --- the Reviewer's first pass: each finding covered for its class ------------------------------


def number_filter(value: Model) -> Model:
    return query(filters=[visual("sales.amount", {"op": "in", "values": [value]})])


def test_exact_values_are_never_rounded() -> None:
    big = "1234567890123456789012345678901234567890"
    bound = bind(number_filter({"type": "integer", "value": big}))
    assert bound.canonical["filters"][0]["predicate"]["values"] == (("number", big),)
    one = bind(number_filter({"type": "decimal", "value": "1.0000000000000000000000000000001"}))
    two = bind(number_filter({"type": "decimal", "value": "1.0000000000000000000000000000002"}))
    assert one.fingerprint != two.fingerprint
    # The member's type applies the scale: integer 1 and decimal 1.0 are one value.
    assert (
        bind(number_filter({"type": "integer", "value": "1"})).fingerprint
        == bind(number_filter({"type": "decimal", "value": "1.000"})).fingerprint
    )

    def at(value: str) -> BoundQuery:
        return bind(
            query(
                filters=[
                    visual(
                        "sales.sold_at",
                        {"op": "range", "lt": {"type": "timestamp", "value": value}},
                    )
                ]
            )
        )

    assert (
        at("2026-01-01T00:00:00.123456789Z").fingerprint
        != at("2026-01-01T00:00:00.123456Z").fingerprint
    )
    assert at("2026-01-01T00:00:00.120Z").fingerprint == at("2026-01-01T00:00:00.12Z").fingerprint


@pytest.mark.parametrize(
    "predicate",
    [
        {"op": "relative_date", "unit": "month", "offset": "-100000", "count": "1"},
        {"op": "relative_date", "unit": "minute", "offset": "0", "count": "999999999999999999"},
        {
            "op": "relative_date",
            "unit": "minute",
            "offset": "-99999999999999999999999",
            "count": "1",
        },
        {"op": "relative_date", "unit": "day", "offset": "-999999999", "count": "1"},
    ],
)
def test_a_relative_date_outside_the_calendar_is_refused(predicate: Model) -> None:
    error = failure(query(filters=[visual("sales.sold_at", predicate)]))
    assert problems(error) == [("schema", "/filters/0/predicate")]


@pytest.mark.parametrize(
    "value",
    [
        {"type": "timestamp", "value": "0001-01-01T00:00:00+05:00"},
        {"type": "timestamp", "value": "9999-12-31T23:00:00-05:00"},
        {"type": "timestamp", "value": "2026-01-01T24:00:00Z"},
    ],
)
def test_an_instant_without_a_utc_form_is_refused(value: Model) -> None:
    error = failure(query(filters=[visual("sales.sold_at", {"op": "range", "lt": value})]))
    assert problems(error) == [("schema", "/filters/0/predicate/lt/value")]


def test_a_lone_surrogate_is_refused() -> None:
    error = failure(
        query(filters=[visual("sales.region", {"op": "in", "values": [text("\ud800")]})])
    )
    assert {rule for rule, _ in problems(error)} == {"schema"}


@pytest.mark.parametrize("value", ["a.b.c", "", "sales.region.", 'x"y', "revenue"])
def test_a_member_parameter_must_name_a_member(value: str) -> None:
    declared = [{"name": "dimension", "type": "member", "default": "sales.region"}]
    error = failure(
        query(parameters=[{"name": "dimension", "value": text(value)}]),
        compiled(model(parameters=declared)),
    )
    assert error.code == "PARAMETER_INVALID"


def test_duplicate_parameters_need_one_value() -> None:
    declared = [{"name": "n", "type": "number", "default": "5"}]
    target = compiled(model(parameters=declared))
    five = {"name": "n", "value": {"type": "integer", "value": "5"}}
    ten = {"name": "n", "value": {"type": "integer", "value": "10"}}
    assert failure(query(parameters=[five, ten]), target).code == "PARAMETER_INVALID"
    assert (
        bind(query(parameters=[five, five]), target).fingerprint
        == bind(query(parameters=[five]), target).fingerprint
    )


@pytest.mark.parametrize(
    ("instant", "unit", "offset", "expected"),
    [
        # New York, fall back: 06:30Z is 01:30 EST, the second 1 a.m.
        ("2026-11-01T06:30:00Z", "hour", 0, ("2026-11-01T06:00:00", "2026-11-01T07:00:00")),
        ("2026-11-01T06:30:00Z", "hour", -1, ("2026-11-01T05:00:00", "2026-11-01T06:00:00")),
        # Spring forward: 07:30Z is 03:30 EDT, right after the missing hour.
        ("2026-03-08T07:30:00Z", "hour", -1, ("2026-03-08T06:00:00", "2026-03-08T07:00:00")),
    ],
)
def test_hours_step_in_elapsed_time_across_daylight_saving(
    instant: str, unit: str, offset: int, expected: tuple[str, str]
) -> None:
    moment = datetime.fromisoformat(instant)
    start, end = relative_range(moment, "America/New_York", unit, offset, 1, False)
    as_utc = (
        start.astimezone(UTC).replace(tzinfo=None).isoformat(),
        end.astimezone(UTC).replace(tzinfo=None).isoformat(),
    )
    assert as_utc == expected


@pytest.mark.parametrize(
    ("instant", "unit", "expected_end"),
    [
        ("2024-03-01T12:00:00+00:00", "year", "2023-03-01T12:00:00"),
        ("2025-03-01T12:00:00+00:00", "year", "2024-03-01T12:00:00"),
        ("2024-02-29T12:00:00+00:00", "year", "2023-02-28T12:00:00"),
        ("2026-05-31T12:00:00+00:00", "quarter", "2026-02-28T12:00:00"),
        ("2026-03-31T12:00:00+00:00", "month", "2026-02-28T12:00:00"),
    ],
)
def test_to_date_cuts_at_the_same_calendar_position(
    instant: str, unit: str, expected_end: str
) -> None:
    _, end = relative_range(datetime.fromisoformat(instant), "UTC", unit, -1, 1, True)
    assert end.replace(tzinfo=None).isoformat() == expected_end


def test_a_metric_filter_cycle_is_found_whatever_the_spelling() -> None:
    def metric_filter(metric: str, dimension: str) -> Model:
        return {
            "scope": "visual",
            "metric": metric,
            "dimension": dimension,
            "condition": {"op": "top", "n": "3"},
        }

    error = failure(
        query(
            metric_filters=[
                metric_filter("revenue", "sales.region"),
                metric_filter("buyers", '"SALES".REGION'),
            ]
        )
    )
    assert error.code == "FILTER_CYCLE"
    same = metric_filter("revenue", "sales.region")
    assert (
        bind(query(metric_filters=[same, same])).fingerprint
        == bind(query(metric_filters=[same])).fingerprint
    )


def test_sets_are_deduplicated_wherever_they_appear() -> None:
    modifier = {"op": "remove", "scope": "page", "members": ["sales.region", "SALES.REGION"]}
    once = {"op": "remove", "scope": "page", "members": ["sales.region"]}
    assert (
        bind(query(context_modifiers=[modifier, once])).fingerprint
        == bind(query(context_modifiers=[once])).fingerprint
    )
    per = {"by": "revenue", "n": "3", "others": False, "per": ["sales.region", "sales.REGION"]}
    assert (
        bind(query(top_n=per)).fingerprint
        == bind(query(top_n={**per, "per": ["sales.region"]})).fingerprint
    )
    contexts = [{"name": "a", "filters": [visual("sales.region", {"op": "is_null"})]}] * 2
    renamed = [contexts[0], {**contexts[0], "name": "A"}]
    assert problems(failure(query(contexts=renamed))) == [("kind", "/contexts/1/name")]


def test_a_value_must_fit_its_members_type() -> None:
    document = query(
        filters=[
            visual("sales.region", {"op": "range", "gte": {"type": "date", "value": "2026-01-01"}})
        ]
    )
    assert problems(failure(document)) == [("kind", "/filters/0/predicate/gte")]


def test_filters_are_ordered_by_scope_then_member() -> None:
    filters = bind(rich()).canonical["filters"]
    keys = [(f["scope"], f["member"]) for f in filters]
    assert keys == sorted(keys)


_EDGE_TEXT = st.one_of(
    st.from_regex(r"-?[0-9]{1,30}", fullmatch=True),
    st.from_regex(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", fullmatch=True),
    st.from_regex(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})?",
        fullmatch=True,
    ),
    st.text(max_size=6),
    st.sampled_from(["sales.region", "revenue", "a.b.c", '"x"', "sales.sold_at"]),
)


def _leaves(value: object, path: tuple[object, ...] = ()) -> Iterator[tuple[object, ...]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, (*path, key))
    elif isinstance(value, list):
        for at, item in enumerate(value):
            yield from _leaves(item, (*path, at))
    elif isinstance(value, str):
        yield path


def fuzz_base() -> Model:
    document = rich()
    document["filters"] += [
        visual(
            "sales.sold_at",
            {"op": "relative_date", "unit": "month", "offset": "-1", "count": "2", "to_date": True},
        ),
        visual("sales.sold_on", {"op": "range", "gte": {"type": "date", "value": "2026-01-01"}}),
        visual(
            "sales.sold_at",
            {"op": "range", "lt": {"type": "timestamp", "value": "2026-01-01T00:00:00Z"}},
        ),
    ]
    document["top_n"] = {"by": "revenue", "n": "3", "others": True}
    document["as_known_at"] = "2026-01-01T00:00:00Z"
    return document


_PATHS = list(_leaves(fuzz_base()))


@settings(max_examples=400, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(_PATHS), _EDGE_TEXT), min_size=1, max_size=4))
def test_any_valid_shape_with_edge_values_gives_a_bound_query_or_a_semantic_error(
    edits: list[tuple[tuple[object, ...], str]],
) -> None:
    """Schema-aware: the query keeps its shape, and leaf texts take edge values."""
    document = fuzz_base()
    for path, value in edits:
        node: Any = document
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    with contextlib.suppress(SemanticError):
        bind(json.dumps(document))
