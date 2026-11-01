"""P2-03 (#278): the 21 corpus models load through the Ossie adapter, in YAML and in JSON.

The imported models are read as written, with two additions from the conformance adapter:
- a metric expression that names other metrics moves, text unchanged, to the product's dialect
  (an Ossie expression cannot name a metric);
- a relationship whose bi-platform entry declares an as-of match gets that rule as a product
  entry too (Owner decision of 2026-11-01, CASE-013). The bi-platform entries stay as they are:
  preserved, listed and never interpreted.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from orvex.semantic import SemanticError
from orvex.semantic.ossie.reader import read

from conformance.runners import bi_platform_adapter as adapter
from conformance.runners.semantic_corpus import CORPUS, ROOT

MODELS = sorted((ROOT / CORPUS / "models").glob("CASE-0??.yaml"))
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
VENDORS = (_PRODUCT["ossie_vendor_name"], *_PRODUCT["previous_vendor_names"])


def corpus_model(path: Path) -> dict[str, Any]:
    raw = adapter.load_yaml(path)
    assert isinstance(raw, dict)
    model = adapter.move_metric_expressions(dict(raw), VENDORS[0])
    translated, _ = adapter.translate(dict(raw), VENDORS[0])
    for written, done in zip(
        model.get("relationships") or [], translated.get("relationships") or [], strict=True
    ):
        product = [e for e in done.get("custom_extensions") or [] if e["vendor_name"] == VENDORS[0]]
        if product and "temporal" in json.loads(product[0]["data"]):
            written["custom_extensions"] = [*(written.get("custom_extensions") or []), product[0]]
    return model


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


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_every_corpus_model_loads_in_yaml_and_json(path: Path) -> None:
    model = corpus_model(path)
    expected = bi_platform_entries(model)
    assert expected
    loaded = []
    for form, content in (
        ("yaml", yaml.safe_dump(model, sort_keys=False).encode()),
        ("json", json.dumps(model).encode()),
    ):
        result = read(content, f"semantic/{path.stem}.ossie.{form}", VENDORS)
        assert [e.pointer for e in result.foreign_extensions] == sorted(
            expected, key=[e.pointer for e in result.foreign_extensions].index
        )
        assert {e.vendor_name for e in result.foreign_extensions} == {adapter.VENDOR_NAME}
        loaded.append(result)
    assert loaded[0] == loaded[1]


def test_the_imported_file_itself_loads_when_it_needs_no_addition() -> None:
    path = ROOT / CORPUS / "models" / "CASE-001.yaml"
    content = path.read_bytes()
    result = read(content, "semantic/CASE-001.ossie.yaml", VENDORS)
    assert result.content == content


def test_case_013_without_its_product_temporal_entry_is_refused() -> None:
    raw = adapter.load_yaml(ROOT / CORPUS / "models" / "CASE-013.yaml")
    assert isinstance(raw, dict)
    model = adapter.move_metric_expressions(dict(raw), VENDORS[0])
    with pytest.raises(SemanticError) as caught:
        read(json.dumps(model).encode(), "semantic/CASE-013.ossie.json", VENDORS)
    (problem,) = cast(list[dict[str, str]], caught.value.details["problems"])
    assert (problem["rule"], problem["pointer"]) == ("reference", "/relationships/0/to_columns")


@pytest.mark.parametrize("path", MODELS, ids=lambda path: path.stem)
def test_every_translated_corpus_model_loads(path: Path) -> None:
    raw = adapter.load_yaml(path)
    assert isinstance(raw, dict)
    translated, _ = adapter.translate(dict(raw), VENDORS[0])
    result = read(json.dumps(translated).encode(), f"semantic/{path.stem}.ossie.json", VENDORS)
    assert result.foreign_extensions == ()
    assert result.product_extensions


def test_moved_expressions_keep_their_text() -> None:
    loaded = adapter.load_yaml(ROOT / CORPUS / "models" / "CASE-009.yaml")
    assert isinstance(loaded, dict)
    raw: dict[str, Any] = dict(loaded)
    before = {m["name"]: m["expression"] for m in raw["metrics"]}
    after = {
        m["name"]: m["expression"]
        for m in adapter.move_metric_expressions(dict(raw), "T")["metrics"]
    }
    moved = {name for name in before if before[name] != after[name]}
    assert moved == {"net_revenue"}
    (text,) = [d["expression"] for d in before["net_revenue"]["dialects"]]
    assert after["net_revenue"] == {"dialects": [{"dialect": "T", "expression": text}]}
