"""The only place that knows bi-platform's corpus format (P0-11).

bi-platform wrote its corpus as prose, CSV files and expected results, and one Apache Ossie model
per case with `custom_extensions` under its own vendor name. This adapter reads that format and
translates what the product needs today: CSV values, the vendor alias and the error codes.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import yaml

VENDOR_NAME = "bi-platform"

# bi-platform error code -> the product's error code (docs/architecture/semantic-engine.md §12).
# Every expected error code of an imported case must appear here; an unmapped code fails the load.
ERROR_CODES = {
    "AMBIGUOUS_RELATIONSHIP_PATH": "AMBIGUOUS_RELATIONSHIP_PATH",
    "ILLEGAL_TEMPORAL_REAGGREGATION": "ILLEGAL_TEMPORAL_REAGGREGATION",
    "INCOMPATIBLE_UNITS": "INCOMPATIBLE_UNITS",
}

# bi-platform manifest outcome -> what the case expects.
OUTCOMES = {"result": "result", "compile_error": "error"}

type Row = dict[str, str | None]
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


def read_csv(path: Path) -> list[Row]:
    """Header row, comma separator, no quoting; an empty field is NULL; values stay text.

    Values are never converted to numbers, so a decimal keeps the scale it was written with.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, quoting=csv.QUOTE_NONE, strict=True)
        header = next(reader)
        rows: list[Row] = []
        for line in reader:
            if len(line) != len(header):
                message = f"{path.name}: a row has {len(line)} fields, the header {len(header)}"
                raise ValueError(message)
            rows.append({name: value or None for name, value in zip(header, line, strict=True)})
    return rows


def load_yaml(path: Path) -> Json:
    loaded: Json = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def vendor_extensions(model: Json) -> list[Json]:
    """Every `custom_extensions` payload written under bi-platform's vendor name.

    They are kept aside until `ossie-extensions` v1 (P2-01) defines the product's own payloads they
    translate to. Entries of any other vendor are ignored, as Ossie requires.
    """
    return list(_walk_extensions(model))


def _walk_extensions(node: Json) -> Iterator[Json]:
    if isinstance(node, dict):
        extensions = node.get("custom_extensions")
        if isinstance(extensions, list):
            for entry in extensions:
                if isinstance(entry, dict) and entry.get("vendor_name") == VENDOR_NAME:
                    yield entry.get("data")
        for value in node.values():
            yield from _walk_extensions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_extensions(item)


def product_error_code(code: str) -> str | None:
    return ERROR_CODES.get(code)
