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
  - CASE-009: `over_metrics` (`gross_revenue - refunds`, the contract's own example) needs a
    declared alignment that the corpus does not give.
  - CASE-006: a ratio of two metrics of one dataset. It has a standard form,
    `CAST(SUM(orders.amount) / COUNT(orders.order_id) AS DECIMAL(18, 2))`, but the original
    is kept, never substituted. As a product construct it needs a declared alignment too.
  - CASE-021: `window` (`running_total` of `daily_revenue` by `date`), not declared in the
    corpus.
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
from orvex.semantic.compiler.parsing import MemberId, parse_model
from orvex.semantic.ossie.reader import read

from conformance.runners import bi_platform_adapter as adapter
from conformance.runners.semantic_corpus import CORPUS, ROOT

MODELS = sorted((ROOT / CORPUS / "models").glob("CASE-0??.yaml"))
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
VENDORS = (_PRODUCT["ossie_vendor_name"], *_PRODUCT["previous_vendor_names"])

# Case -> the metric expression that names another metric.
# Translated by the adapter into an explicit product construct.
EXPLICIT_EXTENSION = {"CASE-010"}
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
    if path.stem in FUTURE and path.stem not in EXPLICIT_EXTENSION:
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
    candidates.append(("json", json.dumps(translated).encode()))
    for form, content in candidates:
        if path.stem in FUTURE and (form == "yaml" or path.stem not in EXPLICIT_EXTENSION):
            continue
        parsed = parse_model(read(content, f"semantic/{path.stem}.ossie.{form}", VENDORS), VENDORS)
        assert parsed.excluded == {}
        if path.stem == "CASE-010" and form == "json":
            attainment = parsed.members[MemberId("metric", None, "ATTAINMENT")]
            assert attainment.language == "product"
            assert [m.name for m in attainment.depends_on] == ["ACTUAL_REVENUE", "TARGET_REVENUE"]
