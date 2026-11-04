"""The imported semantic corpus (`conformance/semantic-corpus/bi-platform`), compiled as the
semantic tests read it: each case's model through the adapter, and its written query (#283)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read

from conformance.runners import bi_platform_adapter as adapter
from tests.support.policies import VENDORS

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "conformance/semantic-corpus/bi-platform/models"
QUERIES: dict[str, Any] = json.loads(
    (ROOT / "conformance/runners/corpus_queries.json").read_text("utf-8")
)["queries"]


def corpus_model(case: str) -> CompiledModel:
    path = MODELS / f"{case}.yaml"
    translated, _ = adapter.translate(yaml.safe_load(path.read_text("utf-8")), VENDORS[0])
    written = yaml.safe_load(path.with_name(f"{case}.catalog.yaml").read_text("utf-8"))
    catalog = {
        source: {column: SourceColumn(*shape) for column, shape in columns.items()}
        for source, columns in adapter.translate_catalog(written).items()
    }
    loaded = read(json.dumps(translated).encode(), f"semantic/{case}.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS)
