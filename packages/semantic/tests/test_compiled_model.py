"""P2-04e (#291; #280 is the source of truth): declarations, the compiled model and its
fingerprints. Every model case runs on the model written as YAML and as JSON."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.compiler.catalog import SourceCatalog, SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.ossie.reader import read
from orvex.semantic.ossie.registry import Registry, default_registry

from conformance.runners import bi_platform_adapter as adapter

ROOT = Path(__file__).resolve().parents[3]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])
FORMS = ("yaml", "json")
CORPUS = ROOT / "conformance/semantic-corpus/bi-platform/models"
GOLDEN = Path(__file__).with_name("golden") / "corpus-fingerprints.json"
# Refused by design: CASE-017 adds USD to BOB in `combined` (P2-16), proven in test_ossie_corpus.
REFUSED = {"CASE-017"}

Model = dict[str, Any]

CATALOG: SourceCatalog = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT", False),
        "amount": SourceColumn("DECIMAL(18,2)"),
        "cost": SourceColumn("DECIMAL(18,2)"),
        "sold_at": SourceColumn("TIMESTAMP WITH TIME ZONE"),
        "store_id": SourceColumn("BIGINT", False),
    },
    "balances": {
        "account_id": SourceColumn("BIGINT", False),
        "day": SourceColumn("DATE", False),
        "balance": SourceColumn("DECIMAL(18,2)"),
    },
    "stores": {"store_id": SourceColumn("BIGINT", False)},
    "store_campaign": {
        "store_id": SourceColumn("BIGINT", False),
        "campaign_id": SourceColumn("BIGINT", False),
        "weight": SourceColumn("DECIMAL(5,4)", False),
    },
    "campaigns": {"campaign_id": SourceColumn("BIGINT", False)},
}


def extension(payload: dict[str, Any]) -> Model:
    return {"vendor_name": CURRENT, "data": json.dumps({"version": 1, **payload})}


def field(name: str, expression: str | None = None, **payload: Any) -> Model:  # noqa: ANN401
    node: Model = {"name": name, "expression": {"sql": expression or name}}
    if payload:
        node["custom_extensions"] = [extension({"element": "field", **payload})]
    return node


def metric(name: str, expression: str, **payload: Any) -> Model:  # noqa: ANN401
    node: Model = {"name": name, "expression": {"sql": expression}}
    if payload:
        node["custom_extensions"] = [extension({"element": "metric", **payload})]
    return node


def base(*metrics: Model) -> Model:
    return {
        "version": "0.2.0.dev0",
        "name": "shop",
        "description": "A shop.",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "description": "One row per sale.",
                "fields": [
                    field("sale_id"),
                    field("customer_id"),
                    field("amount"),
                    field("cost"),
                    field("sold_at"),
                    field("store_id"),
                ],
            },
            {
                "name": "balances",
                "source": "balances",
                "primary_key": ["account_id", "day"],
                "fields": [field("account_id"), field("day"), field("balance")],
                "custom_extensions": [
                    extension({"element": "dataset", "time_roles": {"snapshot": "day"}})
                ],
            },
        ],
        "metrics": list(metrics),
    }


def content(document: Model, form: str) -> bytes:
    if form == "yaml":
        return yaml.safe_dump(document, sort_keys=False).encode()
    return json.dumps(document).encode()


def compile_both(
    document: Model,
    catalog: SourceCatalog = CATALOG,
    **options: Any,  # noqa: ANN401
) -> list[CompiledModel]:
    found = []
    for form in FORMS:
        loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
        found.append(compile_model(loaded, catalog, VENDORS, **options))
    return found


def missing_declaration(document: Model) -> list[str]:
    found = []
    for form in FORMS:
        loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
        with pytest.raises(SemanticError) as caught:
            compile_model(loaded, CATALOG, VENDORS)
        assert caught.value.code == "MISSING_EXTENSION_DECLARATION"
        assert caught.value.details["element"] == "metric"
        found.append(str(caught.value.details["declaration"]))
    return found


def m(name: str) -> MemberId:
    return MemberId("metric", None, name.upper())


# --- the corpus and its golden fingerprints ---------------------------------------------------


def corpus_catalog(path: Path) -> SourceCatalog:
    written = yaml.safe_load(path.with_name(f"{path.stem}.catalog.yaml").read_text("utf-8"))
    return {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }


def corpus_fingerprints() -> dict[str, dict[str, str]]:
    """Every corpus model P2-04 compiles, through the adapter, as JSON and as YAML."""
    found: dict[str, dict[str, str]] = {}
    for path in sorted(CORPUS.glob("CASE-0??.yaml")):
        if path.stem in REFUSED:
            continue
        translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
        prints = set()
        for form in FORMS:
            loaded = read(content(translated, form), f"semantic/{path.stem}.ossie.{form}", VENDORS)
            compiled = compile_model(loaded, corpus_catalog(path), VENDORS)
            prints.add((compiled.semantic_fingerprint, compiled.model_fingerprint))
        assert len(prints) == 1, path.stem
        semantic, whole = prints.pop()
        found[path.stem] = {"semantic_fingerprint": semantic, "model_fingerprint": whole}
    return found


def test_the_corpus_compiles_to_its_golden_fingerprints() -> None:
    """To refresh after an intended change, write `corpus_fingerprints()` to the golden file and
    review the diff: every changed fingerprint is a changed meaning."""
    golden = json.loads(GOLDEN.read_text("utf-8"))
    # The 17 models P2-03 accepts, CASE-006, CASE-009, CASE-010 and CASE-021 translated, less
    # CASE-017 (refused).
    assert len(golden) == 20
    assert corpus_fingerprints() == golden


def test_fingerprints_are_equal_across_processes_and_hash_seeds() -> None:
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from packages.semantic.tests import test_compiled_model as t; "
        "print(json.dumps(t.corpus_fingerprints(), sort_keys=True))"
    )
    golden = json.loads(GOLDEN.read_text("utf-8"))
    for seed in ("0", "1", "4242"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env=environment,
        )
        assert json.loads(run.stdout) == golden


def test_case_019s_zone_is_in_the_fingerprint() -> None:
    path = CORPUS / "CASE-019.yaml"
    translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
    golden = json.loads(GOLDEN.read_text("utf-8"))["CASE-019"]["semantic_fingerprint"]
    moved = json.loads(json.dumps(translated).replace("America/La_Paz", "America/Lima"))
    loaded = read(content(moved, "json"), "semantic/CASE-019.ossie.json", VENDORS)
    assert compile_model(loaded, corpus_catalog(path), VENDORS).semantic_fingerprint != golden


# --- declarations: the compile-time classes (#280 §3) -----------------------------------------


def test_a_ratio_of_aggregates_needs_its_declarations_in_order() -> None:
    steps: list[tuple[dict[str, Any], str | None]] = [
        ({}, "additivity"),
        ({"additivity": "additive"}, "additivity"),  # a ratio is recomputed or non-additive
        ({"additivity": "non_additive"}, "zero_denominator"),
        ({"additivity": "non_additive", "zero_denominator": "null"}, "rounding"),
    ]
    for payload, declaration in steps:
        document = base(metric("margin", "SUM(sales.amount) / SUM(sales.cost)", **payload))
        assert missing_declaration(document) == [declaration, declaration]
    complete = {
        "additivity": "recomputable",
        "components": {"numerator": "SUM(sales.amount)", "denominator": "SUM(sales.cost)"},
        "zero_denominator": "null",
        "rounding": {"scale": 4, "mode": "half_even"},
    }
    for compiled in compile_both(
        base(metric("margin", "SUM(sales.amount) / SUM(sales.cost)", **complete))
    ):
        assert compiled.members[m("margin")].missing == ()


def test_an_average_over_exact_inputs_is_a_ratio_class() -> None:
    assert missing_declaration(base(metric("mean", "AVG(sales.amount)"))) == ["additivity"] * 2
    declared = {
        "additivity": "non_additive",
        "zero_denominator": "null",
        "rounding": {"scale": 4, "mode": "half_even"},
    }
    assert compile_both(base(metric("mean", "AVG(sales.amount)", **declared)))


def test_a_holistic_aggregate_needs_additivity_and_aggregate_class() -> None:
    text = "COUNT(DISTINCT sales.customer_id)"
    assert missing_declaration(base(metric("buyers", text))) == ["additivity"] * 2
    document = base(metric("buyers", text, additivity="non_additive"))
    assert missing_declaration(document) == ["aggregate_class"] * 2
    declared = base(metric("buyers", text, additivity="non_additive", aggregate_class="holistic"))
    for compiled in compile_both(declared):
        assert compiled.members[m("buyers")].missing == ()


def test_an_approximate_aggregate_also_needs_approximate() -> None:
    text = "APPROX_COUNT_DISTINCT(sales.customer_id)"
    declared: dict[str, Any] = {"additivity": "non_additive", "aggregate_class": "sketch"}
    assert missing_declaration(base(metric("buyers", text, **declared))) == ["approximate"] * 2
    declared["approximate"] = {"error_bound": "0.02"}
    assert compile_both(base(metric("buyers", text, **declared)))


def test_an_aggregate_over_a_snapshot_needs_additivity() -> None:
    assert missing_declaration(base(metric("total", "SUM(balances.balance)"))) == ["additivity"] * 2
    axes = [{"behavior": "forbidden", "dimension": "day"}]
    declared = base(
        metric("total", "SUM(balances.balance)", additivity="semi_additive", non_additive_axes=axes)
    )
    assert compile_both(declared)


def bridged(*metrics: Model) -> Model:
    document = base(*metrics)
    document["datasets"] += [
        {
            "name": "stores",
            "source": "stores",
            "primary_key": ["store_id"],
            "fields": [field("store_id")],
        },
        {
            "name": "store_campaign",
            "source": "store_campaign",
            "primary_key": ["store_id", "campaign_id"],
            "fields": [field("store_id"), field("campaign_id"), field("weight")],
            "custom_extensions": [
                extension(
                    {
                        "element": "dataset",
                        "bridge": {
                            "left": "store",
                            "right": "campaign",
                            "weight": "weight",
                            "total": "1.00",
                            "tolerance": "0.00",
                            "normalization_scope": {"entities": ["store"]},
                        },
                    }
                )
            ],
        },
        {
            "name": "campaigns",
            "source": "campaigns",
            "primary_key": ["campaign_id"],
            "fields": [field("campaign_id")],
        },
    ]
    document["relationships"] = [
        {
            "name": name,
            "from": "store_campaign",
            "to": target,
            "from_columns": [column],
            "to_columns": [column],
        }
        for name, target, column in (
            ("store", "stores", "store_id"),
            ("campaign", "campaigns", "campaign_id"),
        )
    ]
    return document


def test_a_metric_over_a_bridge_needs_allocation() -> None:
    text = "SUM(store_campaign.weight)"
    assert missing_declaration(bridged(metric("weights", text))) == ["allocation"] * 2


def test_a_plain_sum_compiles_with_additivity_missing() -> None:
    for compiled in compile_both(base(metric("revenue", "SUM(sales.amount)"))):
        assert compiled.members[m("revenue")].missing == ("additivity",)


def test_answerability_lists_every_other_missing_declaration() -> None:
    document = base(metric("revenue", "SUM(sales.amount)", additivity="additive"))
    sales = document["datasets"][0]
    sales["fields"] += [
        field("unit_price", "amount / 3"),
        field("day", "DATE_TRUNC('DAY', sold_at)"),
    ]
    sales["metrics"] = [
        {
            "name": "opaque",
            "datatype": "Decimal",
            "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "sum(amount)"}]},
        },
        {
            "name": "untyped",
            "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "sum(amount)"}]},
        },
    ]
    declared = _declare(document, {"additivity": "non_additive", "aggregate_class": "holistic"})
    for form in FORMS:
        loaded = read(
            content(declared, form),
            f"semantic/shop.ossie.{form}",
            VENDORS,
            {"sales": "DUCKDB"},
        )
        compiled = compile_model(loaded, CATALOG, VENDORS)
        members = compiled.members
        assert members[MemberId("field", "SALES", "UNIT_PRICE")].missing == (
            "rounding",
            "zero_denominator",
        )
        assert members[MemberId("field", "SALES", "DAY")].missing == ("time_zone",)
        assert members[MemberId("metric", "SALES", "OPAQUE")].missing == ("numeric",)
        assert members[MemberId("metric", "SALES", "UNTYPED")].missing == ("datatype",)
        assert members[m("revenue")].missing == ()


def _declare(document: Model, payload: dict[str, Any]) -> Model:
    """The engine-dialect metrics are holistic (#280): declare what that class needs."""
    declared = copy.deepcopy(document)
    for node in declared["datasets"][0]["metrics"]:
        node["custom_extensions"] = [extension({"element": "metric", **payload})]
    return declared


# --- registry entries and identity ------------------------------------------------------------


def registry_with(**changes: Any) -> Registry:  # noqa: ANN401
    entry = default_registry().entries[-1]
    return Registry((replace(entry, **changes),))


def compiled_with(registry: Registry, **options: Any) -> CompiledModel:  # noqa: ANN401
    loaded = read(
        content(base(metric("revenue", "SUM(sales.amount)")), "json"),
        "semantic/shop.ossie.json",
        VENDORS,
        registry=registry,
    )
    return compile_model(loaded, CATALOG, VENDORS, **options)


def test_only_executable_entries_compile() -> None:
    for state in ("read_only", "experimental"):
        with pytest.raises(SemanticError) as caught:
            compiled_with(registry_with(state=state))
        assert caught.value.code == "OSSIE_INVALID"
        problems = cast(list[dict[str, str]], caught.value.details["problems"])
        assert [(p["rule"], p["pointer"]) for p in problems] == [
            ("unsupported_capability", "/version")
        ]
    assert compiled_with(registry_with(state="experimental"), experimental=True)
    loaded = read(content(base(), "json"), "semantic/shop.ossie.json", VENDORS)
    with pytest.raises(SemanticError):
        compile_model(replace(loaded, registry_state="rejected"), CATALOG, VENDORS)


def test_two_revisions_of_one_reader_give_equal_fingerprints() -> None:
    first = compiled_with(registry_with(id="rev-a", commit="a" * 40))
    second = compiled_with(registry_with(id="rev-b", commit="b" * 40))
    assert (first.registry_entry, second.registry_entry) == ("rev-a", "rev-b")
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.model_fingerprint == second.model_fingerprint
    raised = compiled_with(registry_with(id="rev-c", capabilities=second.capabilities + 1))
    assert raised.semantic_fingerprint != second.semantic_fingerprint
    assert raised.model_fingerprint != second.model_fingerprint


def test_the_compiled_model_keeps_every_advisory() -> None:
    path = CORPUS / "CASE-013.yaml"
    translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
    loaded = read(content(translated, "json"), "semantic/CASE-013.ossie.json", VENDORS)
    compiled = compile_model(loaded, corpus_catalog(path), VENDORS)
    assert compiled.advisories == loaded.advisories
    assert "to_columns_cover_no_key" in {a.kind for a in compiled.advisories}
    document = base(metric("revenue", "SUM(sales.amount)"))
    document["metrics"].append(
        {"name": "odd", "expression": {"dialects": [{"dialect": "NOPE", "expression": "x"}]}}
    )
    for compiled in compile_both(document):
        assert "no_executable_expression" in {a.kind for a in compiled.advisories}
        assert compiled.excluded == {m("odd"): "expression_dialect"}
        with pytest.raises(SemanticError) as caught:
            compiled.require(m("odd"))
        assert caught.value.code == "UNSUPPORTED_OPERATION"


# --- properties --------------------------------------------------------------------------------


def fingerprints(document: Model, form: str = "json") -> tuple[str, str]:
    loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
    compiled = compile_model(loaded, CATALOG, VENDORS)
    return compiled.semantic_fingerprint, compiled.model_fingerprint


def reference() -> Model:
    return base(
        metric("revenue", "SUM(sales.amount)", additivity="additive", unit="1"),
        metric("cost", "SUM(sales.cost) FILTER (WHERE sales.store_id > 3)"),
    )


def _shuffle_keys(value: object, order: list[int]) -> object:
    if isinstance(value, dict):
        keys = list(value)
        keys = keys[order[0] % len(keys) :] + keys[: order[0] % len(keys)] if keys else keys
        return {key: _shuffle_keys(value[key], order[1:] or order) for key in keys}
    if isinstance(value, list):
        return [_shuffle_keys(item, order) for item in value]
    return value


def named(document: Model, name: str, collection: str = "metrics") -> Model:
    return next(item for item in document[collection] if item["name"] == name)


EDITS: dict[str, Callable[[Model], object]] = {
    "reorder datasets": lambda d: d["datasets"].reverse(),
    "reorder fields": lambda d: named(d, "sales", "datasets")["fields"].reverse(),
    "reorder metrics": lambda d: d["metrics"].reverse(),
    "whitespace": lambda d: named(d, "cost")["expression"].update(
        sql="SUM( sales.cost )\n  FILTER (WHERE   sales.store_id>3)"
    ),
    "keyword case": lambda d: named(d, "cost")["expression"].update(
        sql="sum(sales.cost) filter (where sales.store_id > 3)"
    ),
    "identifier case": lambda d: named(d, "revenue")["expression"].update(sql="SUM(SALES.AMOUNT)"),
    "description": lambda d: named(d, "sales", "datasets").update(
        description="Every sale, one row each."
    ),
}
MUTATIONS: dict[str, Callable[[Model], object]] = {
    "aggregate": lambda d: named(d, "revenue")["expression"].update(sql="MAX(sales.amount)"),
    "filter": lambda d: named(d, "cost")["expression"].update(
        sql="SUM(sales.cost) FILTER (WHERE sales.store_id > 4)"
    ),
    "column": lambda d: named(d, "revenue")["expression"].update(sql="SUM(sales.cost)"),
    "datatype": lambda d: named(d, "revenue").update(datatype="Decimal"),
    "declaration": lambda d: named(d, "revenue")["custom_extensions"].__setitem__(
        0, extension({"element": "metric", "additivity": "non_additive", "unit": "1"})
    ),
    "unit": lambda d: named(d, "revenue")["custom_extensions"].__setitem__(
        0, extension({"element": "metric", "additivity": "additive", "unit": "%"})
    ),
    "primary key": lambda d: named(d, "sales", "datasets").update(
        primary_key=["sale_id", "store_id"]
    ),
    "new field": lambda d: named(d, "sales", "datasets")["fields"].append(field("again", "amount")),
}


@settings(max_examples=40, deadline=None)
@given(
    st.lists(st.sampled_from(sorted(EDITS)), max_size=len(EDITS), unique=True),
    st.lists(st.integers(0, 9), min_size=1, max_size=4),
    st.sampled_from(FORMS),
)
def test_edits_that_cannot_change_meaning_keep_the_semantic_fingerprint(
    edits: list[str], order: list[int], form: str
) -> None:
    original = fingerprints(reference())
    edited = reference()
    for name in edits:
        EDITS[name](edited)
    edited = cast(Model, _shuffle_keys(edited, order))  # YAML and JSON key order
    semantic, whole = fingerprints(edited, form)
    assert semantic == original[0]
    # The model fingerprint covers the text as written too; reordering alone keeps it.
    written = {"description", "whitespace", "keyword case", "identifier case"}
    assert (whole == original[1]) == (not written & set(edits))


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_any_semantic_mutation_changes_the_semantic_fingerprint(mutation: str) -> None:
    mutated = reference()
    MUTATIONS[mutation](mutated)
    assert fingerprints(mutated)[0] != fingerprints(reference())[0]


_TEXTS = st.one_of(
    st.sampled_from(
        [
            "SUM(sales.amount)",
            "SUM(sales.amount) / SUM(sales.cost)",
            "COUNT(DISTINCT sales.customer_id)",
            "AVG(sales.cost)",
            "SUM(balances.balance)",
            "SUM(sales.amount + 1e40)",
            "SUM(SUM(sales.amount))",
            "MAX(DATE_TRUNC('DAY', sales.sold_at))",
            "APPROX_COUNT_DISTINCT(sales.customer_id)",
            "SUM(sales.amount) FILTER (WHERE sales.cost)",
        ]
    ),
    st.text(max_size=40),
)
_PAYLOADS = st.fixed_dictionaries(
    {},
    optional={
        "additivity": st.sampled_from(
            ["additive", "non_additive", "recomputable", "semi_additive"]
        ),
        "aggregate_class": st.sampled_from(["distributive", "holistic", "sketch"]),
        "zero_denominator": st.sampled_from(["null", "reject"]),
        "rounding": st.just({"scale": 2, "mode": "half_even"}),
        "unit": st.sampled_from(["1", "kg", "xyz"]),
    },
)


@settings(max_examples=150, deadline=None)
@given(_TEXTS, _PAYLOADS)
def test_any_input_gives_a_model_or_a_semantic_error(text: str, payload: dict[str, Any]) -> None:
    document = base(metric("x", text, **payload) if payload else metric("x", text))
    try:
        loaded = read(content(document, "json"), "semantic/shop.ossie.json", VENDORS)
        compiled = compile_model(loaded, CATALOG, VENDORS)
    except SemanticError:
        return
    assert compiled.semantic_fingerprint.startswith("sha256:")


# --- the Reviewer's first pass: each finding covered for its class ------------------------------


def test_a_ratio_stays_a_ratio_one_level_up() -> None:
    ratio = {
        "additivity": "non_additive",
        "zero_denominator": "null",
        "rounding": {"scale": 4, "mode": "half_even"},
    }
    for derived_text, base_text in (
        ("r * 1", "SUM(sales.amount) / SUM(sales.cost)"),
        ("r + r", "SUM(sales.amount) / SUM(sales.cost)"),
        ("r + 0", "AVG(sales.amount)"),
    ):
        document = base(metric("r", base_text, **ratio))
        document["metrics"].append(derived("r2", derived_text, additivity="additive"))
        assert missing_declaration(document) == ["additivity"] * 2
        document["metrics"][-1] = derived("r2", derived_text, **ratio)
        assert compile_both(document)


def derived(name: str, text: str, **payload: Any) -> Model:  # noqa: ANN401
    data = {
        "element": "metric",
        "over_metrics": {"expression": text, "align": {"grain": ["month"], "time": "month"}},
        **payload,
    }
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": CURRENT, "expression": text}]},
        "custom_extensions": [extension(data)],
    }


@pytest.mark.parametrize("value", [1.5, 9007199254740993, -9007199254740993, "1e400"])
def test_any_number_in_a_document_gives_a_model(value: object) -> None:
    document = base(metric("revenue", "SUM(sales.amount)"))
    document["datasets"][0]["ai_context"] = {"x": value}
    for compiled in compile_both(document):
        assert compiled.model_fingerprint.startswith("sha256:")


def test_a_member_fingerprint_covers_what_it_reads_beyond_its_expression() -> None:
    def member_print(document: Model, catalog: SourceCatalog = CATALOG) -> str:
        loaded = read(content(document, "json"), "semantic/shop.ossie.json", VENDORS)
        return compile_model(loaded, catalog, VENDORS).members[m("revenue")].semantic_fingerprint

    original = base(metric("revenue", "SUM(sales.amount)"))
    moved = copy.deepcopy(original)
    moved["datasets"][0]["source"] = "sales2"
    catalog = {**CATALOG, "sales2": CATALOG["sales"]}
    assert member_print(moved, catalog) != member_print(original)
    keyed = copy.deepcopy(original)
    keyed["datasets"][0]["primary_key"] = ["sale_id", "store_id"]
    assert member_print(keyed) != member_print(original)
    # A dependent sees the change through its dependency.
    for document in (original, moved):
        document["metrics"].append(derived("doubled", "revenue * 2"))
    first = read(content(original, "json"), "semantic/a.ossie.json", VENDORS)
    second = read(content(moved, "json"), "semantic/b.ossie.json", VENDORS)
    doubled = MemberId("metric", None, "DOUBLED")
    assert (
        compile_model(first, CATALOG, VENDORS).members[doubled].semantic_fingerprint
        != compile_model(second, catalog, VENDORS).members[doubled].semantic_fingerprint
    )


def test_the_model_fingerprint_covers_everything_else() -> None:
    original = base(metric("revenue", "SUM(sales.amount)"))
    original["metrics"][0]["expression"] = {
        "dialects": [
            {"dialect": "OSSIE_SQL_2026", "expression": "SUM(sales.amount)"},
            {"dialect": "DUCKDB", "expression": "sum(sales.amount)"},
        ]
    }
    other_dialect = copy.deepcopy(original)
    other_dialect["metrics"][0]["expression"]["dialects"][1]["expression"] = "sum(amount)"
    renamed = copy.deepcopy(original)
    renamed["name"] = "store"
    for edited in (other_dialect, renamed):
        semantic, whole = fingerprints(edited)
        assert semantic == fingerprints(original)[0]
        assert whole != fingerprints(original)[1]


def test_unordered_payload_collections_are_sorted() -> None:
    axes = [{"behavior": "forbidden", "dimension": "day"}, {"behavior": "additive", "entity": "a"}]
    payload = {"additivity": "semi_additive", "non_additive_axes": axes}
    document = base(metric("total", "SUM(balances.balance)", **payload))
    reordered = base(
        metric("total", "SUM(balances.balance)", **{**payload, "non_additive_axes": axes[::-1]})
    )
    for model in (document, reordered):
        model["datasets"][1]["custom_extensions"] = [
            extension({"element": "dataset", "entity": "a", "time_roles": {"snapshot": "day"}})
        ]
    assert fingerprints(document)[0] == fingerprints(reordered)[0]


def test_payload_conditions_are_p2_03s_own() -> None:
    """`components` with `recomputable`, axes with `semi_additive` and `period_end` with a
    period-end axis are refused by P2-03 before compiling (`ossie-extensions/v1`), so they never
    reach `MISSING_EXTENSION_DECLARATION`."""
    for payload in (
        {"additivity": "recomputable"},
        {"additivity": "semi_additive"},
        {
            "additivity": "semi_additive",
            "non_additive_axes": [{"behavior": "period_end", "dimension": "day"}],
        },
    ):
        document = base(metric("x", "SUM(balances.balance)", **payload))
        for form in FORMS:
            with pytest.raises(SemanticError) as caught:
                read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
            assert caught.value.code == "OSSIE_INVALID"
            problems = cast(list[dict[str, str]], caught.value.details["problems"])
            assert {p["rule"] for p in problems} == {"extension"}


def test_the_first_ambiguous_metric_is_the_same_in_yaml_and_json() -> None:
    document = base(*(metric(f"ok{i}", "SUM(sales.amount)") for i in range(10)))
    document["metrics"].insert(2, metric("bad2", "COUNT(DISTINCT sales.customer_id)"))
    document["metrics"].append(metric("bad10", "AVG(sales.amount)"))
    for form in FORMS:
        loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
        with pytest.raises(SemanticError) as caught:
            compile_model(loaded, CATALOG, VENDORS)
        assert caught.value.details["name"] == "bad2"


def test_requiring_an_unknown_member_is_member_not_found() -> None:
    for compiled in compile_both(base(metric("revenue", "SUM(sales.amount)"))):
        with pytest.raises(SemanticError) as caught:
            compiled.require(m("nothing"))
        assert caught.value.code == "MEMBER_NOT_FOUND"
