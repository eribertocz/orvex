"""P2-03 (#278): the 21 corpus models through the Ossie adapter, in YAML and in JSON.

Every model is read exactly as imported; nothing is rewritten to make it pass. Against the pinned
revision (`c721ffc`, Owner policy of 2026-11-01):

- **Conformant** (17): accepted by P2-03's checks; they load, and their bi-platform entries are
  preserved, listed and never interpreted.
- **Not conformant** (4): a metric expression names another metric, which this revision does
  not support ("a metric expression that refers to another metric", spec L692-L699; L424).
  They are refused with `reference` at that expression, unchanged, until a revision
  specifies derived metrics.
  The product's explicit construct for them is a derived metric (ossie-extensions v1:
  `over_metrics` or `window`, with the expression under the product's dialect token):
  - CASE-010: `over_metrics`, aligned on the grain bi-platform declares. The conformance
    adapter's translation writes it explicitly, so the translated model loads.
  - CASE-009: `over_metrics` (`gross_revenue - refunds`, the contract's own example). The corpus
    gives no alignment; the adapter declares month x region, from the case's README and expected
    file (P2-17), so the translated model loads.
  - CASE-006: a ratio of two metrics of one dataset. The imported file keeps the original. The
    adapter's translation declares it as a ratio (`recomputable`, its `components`, rounding half
    away from zero, empty for a zero denominator; Owner decisions of 2026-11-04, #325) and writes
    its expression with the metrics' own aggregates, so the translated model loads.
  - CASE-021: `window` (`running_total` of `daily_revenue` by `date`), not declared in the
    corpus. The adapter declares it, with the metric it is over as its product expression
    (P2-20), so the translated model loads.
- **CASE-013** is one of the conformant models: the official validator only warns when
  `to_columns` cover no key of `to`. It loads with an advisory; joining through that relationship
  needs the product's own as-of declaration, checked when it is used (P2-20). The test also adds
  that declaration, beside the preserved bi-platform entry, and the model still loads.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from orvex.semantic import SemanticError
from orvex.semantic.compiler.catalog import LogicalType, SourceColumn
from orvex.semantic.compiler.parsing import MemberId, parse_model
from orvex.semantic.compiler.typecheck import type_model
from orvex.semantic.ossie.reader import read

from conformance.runners import bi_platform_adapter as adapter
from conformance.runners.semantic_corpus import CORPUS, ROOT

MODELS = sorted((ROOT / CORPUS / "models").glob("CASE-0??.yaml"))
# Translated with a metric its case expects refused (CASE-017's `combined`, P2-16): proven by
# its own test; the model as imported still types.
REFUSED_AS_TRANSLATED = frozenset({"CASE-017"})
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
VENDORS = (_PRODUCT["ossie_vendor_name"], *_PRODUCT["previous_vendor_names"])

# Case -> the metric expression that names another metric.
# Translated by the adapter into an explicit product construct.
EXPLICIT_EXTENSION = {"CASE-009", "CASE-010", "CASE-021"}
# Translated by the adapter into a declared ratio whose expression is standard Ossie SQL.
DECLARED_RATIO = {"CASE-006"}
FUTURE = {
    "CASE-006": "/metrics/2/expression/dialects/0/expression",
    "CASE-009": "/metrics/2/expression/dialects/0/expression",
    "CASE-010": "/metrics/2/expression/dialects/0/expression",
    "CASE-021": "/metrics/1/expression/dialects/0/expression",
}
CONFORMANT = [p for p in MODELS if p.stem not in FUTURE]


def raw(path: Path) -> dict[str, Any]:
    model = adapter.load_yaml(path)
    assert isinstance(model, dict)
    return dict(model)


def both_formats(path: Path, model: dict[str, Any] | None = None) -> list[tuple[str, bytes]]:
    """The imported file as written, and the same model as JSON."""
    yaml_bytes = path.read_bytes() if model is None else yaml.safe_dump(model).encode()
    return [("yaml", yaml_bytes), ("json", json.dumps(model or raw(path)).encode())]


def bi_platform_entries(model: object, at: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(model, dict):
        for number, entry in enumerate(model.get("custom_extensions") or []):
            if entry["vendor_name"] == adapter.VENDOR_NAME:
                found.append(f"{at}/custom_extensions/{number}")
        for key, value in model.items():
            if key != "custom_extensions":
                found += bi_platform_entries(value, f"{at}/{key}")
    elif isinstance(model, list):
        for number, item in enumerate(model):
            found += bi_platform_entries(item, f"{at}/{number}")
    return found


def test_the_corpus_splits_17_conformant_and_4_not_conformant() -> None:
    assert len(MODELS) == 21
    assert len(CONFORMANT) == 17


@pytest.mark.parametrize("path", CONFORMANT, ids=lambda path: path.stem)
def test_every_conformant_model_loads_unchanged(path: Path) -> None:
    expected = sorted(bi_platform_entries(raw(path)))
    assert expected
    loaded = []
    for form, content in both_formats(path):
        result = read(content, f"semantic/{path.stem}.ossie.{form}", VENDORS)
        assert sorted(e.pointer for e in result.foreign_extensions) == expected
        assert {e.vendor_name for e in result.foreign_extensions} == {adapter.VENDOR_NAME}
        assert result.product_extensions == ()
        loaded.append(result)
    assert loaded[0].content == path.read_bytes()
    assert loaded[0] == loaded[1]


@pytest.mark.parametrize("case", sorted(FUTURE))
def test_a_metric_naming_a_metric_is_refused_unchanged(case: str) -> None:
    path = ROOT / CORPUS / "models" / f"{case}.yaml"
    for form, content in both_formats(path):
        with pytest.raises(SemanticError) as caught:
            read(content, f"semantic/{case}.ossie.{form}", VENDORS)
        (problem,) = cast(list[dict[str, str]], caught.value.details["problems"])
        assert (problem["rule"], problem["pointer"]) == ("reference", FUTURE[case])


def with_product_temporal(path: Path) -> dict[str, Any]:
    """The model with its as-of rule also declared as a product entry, taken from the
    conformance adapter's translation; the bi-platform entry stays as written."""
    model = raw(path)
    translated, _ = adapter.translate(raw(path), VENDORS[0])
    for written, done in zip(model["relationships"], translated["relationships"], strict=True):
        product = [e for e in done["custom_extensions"] if e["vendor_name"] == VENDORS[0]]
        if product and "temporal" in json.loads(product[0]["data"]):
            written["custom_extensions"] = [*written["custom_extensions"], product[0]]
    return model


def test_case_013_loads_as_written_with_its_advisory() -> None:
    """Valid Ossie: the official validator only warns that `to_columns` cover no key of `to`.
    It loads; joining through that relationship needs the product's as-of declaration (P2-20)."""
    path = ROOT / CORPUS / "models" / "CASE-013.yaml"
    for form, content in both_formats(path):
        result = read(content, f"semantic/CASE-013.ossie.{form}", VENDORS)
        assert [(a.pointer, a.kind) for a in result.advisories] == [
            ("/relationships/0/to_columns", "to_columns_cover_no_key")
        ]
        assert result.product_extensions == ()
    model = with_product_temporal(path)
    for form, content in both_formats(path, model):
        result = read(content, f"semantic/CASE-013.ossie.{form}", VENDORS)
        assert sorted(e.pointer for e in result.foreign_extensions) == sorted(
            bi_platform_entries(model)
        )
        assert [e.element for e in result.product_extensions] == ["relationship"]


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_translated_models_follow_the_split_except_explicit_extensions(path: Path) -> None:
    translated, _ = adapter.translate(raw(path), VENDORS[0])
    content = json.dumps(translated).encode()
    if path.stem in FUTURE and path.stem not in EXPLICIT_EXTENSION | DECLARED_RATIO:
        with pytest.raises(SemanticError) as caught:
            read(content, f"semantic/{path.stem}.ossie.json", VENDORS)
        (problem,) = cast(list[dict[str, str]], caught.value.details["problems"])
        assert (problem["rule"], problem["pointer"]) == ("reference", FUTURE[path.stem])
        return
    result = read(content, f"semantic/{path.stem}.ossie.json", VENDORS)
    assert result.foreign_extensions == ()
    assert result.product_extensions
    if path.stem in EXPLICIT_EXTENSION:
        (derived,) = [
            e for e in result.expressions if e.element == FUTURE[path.stem].split("/expression")[0]
        ]
        assert derived.dialect == VENDORS[0]


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_every_accepted_corpus_model_parses_with_nothing_excluded(path: Path) -> None:
    """P2-04b (#288): every corpus model P2-03 accepts, as imported or as translated, parses;
    every reference resolves, and no member is left out."""
    candidates = [("yaml", path.read_bytes())]
    translated, _ = adapter.translate(raw(path), VENDORS[0])
    if path.stem not in REFUSED_AS_TRANSLATED:
        candidates.append(("json", json.dumps(translated).encode()))
    for form, content in candidates:
        if path.stem in FUTURE and (
            form == "yaml" or path.stem not in EXPLICIT_EXTENSION | DECLARED_RATIO
        ):
            continue
        parsed = parse_model(read(content, f"semantic/{path.stem}.ossie.{form}", VENDORS), VENDORS)
        assert parsed.excluded == {}
        if path.stem == "CASE-010" and form == "json":
            attainment = parsed.members[MemberId("metric", None, "ATTAINMENT")]
            assert attainment.language == "product"
            assert [m.name for m in attainment.depends_on] == ["ACTUAL_REVENUE", "TARGET_REVENUE"]


def catalog(path: Path) -> dict[str, dict[str, SourceColumn]]:
    """The case's `.catalog.yaml`, through the adapter, as the store would report it."""
    written = yaml.safe_load(path.with_name(f"{path.stem}.catalog.yaml").read_text("utf-8"))
    return {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_every_accepted_corpus_model_types(path: Path) -> None:
    """P2-04c (#289): every corpus model P2-03 accepts, as imported or as translated, types
    against its catalog; nothing becomes a float. CASE-002 is Decimal(38,2) exact, and CASE-019
    buckets its instants in America/La_Paz."""
    candidates = [("yaml", path.read_bytes())]
    translated, _ = adapter.translate(raw(path), VENDORS[0])
    if path.stem not in REFUSED_AS_TRANSLATED:
        candidates.append(("json", json.dumps(translated).encode()))
    for form, content in candidates:
        if path.stem in FUTURE and (
            form == "yaml" or path.stem not in EXPLICIT_EXTENSION | DECLARED_RATIO
        ):
            continue
        loaded = read(content, f"semantic/{path.stem}.ossie.{form}", VENDORS)
        parsed = parse_model(loaded, VENDORS)
        typed = type_model(loaded, parsed, catalog(path))
        assert set(typed.members) == set(parsed.members)
        assert all(t.type.exactness == "exact" for t in typed.members.values())
        if path.stem == "CASE-002":
            revenue = typed.members[MemberId("metric", None, "REVENUE")]
            assert revenue.type.logical == LogicalType("Decimal", 38, 2)
        if path.stem == "CASE-019" and form == "json":
            day = typed.members[MemberId("field", "EVENTS", "BUSINESS_DAY")]
            assert day.type.logical == LogicalType("Date")  # the local day it starts on (P2-19)
            assert day.tree is not None
            assert day.tree.type.bucket_zone == "America/La_Paz"


def test_the_catalog_adapter_maps_every_corpus_type_and_refuses_an_unknown_one() -> None:
    written = {"t": {"a": "int64", "b": "decimal(18,2) null", "c": "timestamptz", "d": "utf8"}}
    assert adapter.translate_catalog(written) == {
        "t": {
            "a": ("BIGINT", False),
            "b": ("DECIMAL(18,2)", True),
            "c": ("TIMESTAMP WITH TIME ZONE", False),
            "d": ("VARCHAR", False),
        }
    }
    with pytest.raises(ValueError, match="float32"):
        adapter.translate_catalog({"t": {"a": "float32"}})


def test_case_017_combined_revenue_is_incompatible_units() -> None:
    """P2-04d (#290), P2-16 (#331): the case's `combined = revenue_usd + revenue_bob`, which the
    adapter declares with each metric's own aggregate, is refused before execution, with no
    conversion."""
    path = ROOT / CORPUS / "models" / "CASE-017.yaml"
    translated, _ = adapter.translate(raw(path), VENDORS[0])
    (combined,) = [m for m in translated["metrics"] if m["name"] == "combined"]
    text = combined["expression"]["dialects"][0]["expression"]
    assert text == "SUM(finance.amount_usd) + SUM(finance.amount_bob)"
    for form, content in (
        ("json", json.dumps(translated).encode()),
        ("yaml", yaml.safe_dump(translated, sort_keys=False).encode()),
    ):
        loaded = read(content, f"semantic/CASE-017.ossie.{form}", VENDORS)
        parsed = parse_model(loaded, VENDORS)
        with pytest.raises(SemanticError) as caught:
            type_model(loaded, parsed, catalog(path))
        assert caught.value.code == "INCOMPATIBLE_UNITS"
        assert list(cast(list[str], caught.value.details["units"])) == ["USD", "BOB"]
