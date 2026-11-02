"""Ossie export with a loss report (P2-10, #309; ADR-0030 §5).

- **`with_extensions`** gives back the document's own bytes: lossless, with every product
  payload (ADR-0030 §2, preservation).
- **`standard_only`** drops every product payload and reports each dropped declaration, with
  what an Ossie-only reader would then do differently.

Only a builder exports, and only the model: never `security.yaml`, a policy or any data
(Owner decision of 2026-11-02). A conversion to another registry entry, or one that would
change meaning, is refused.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from orvex.semantic.compiler.compiled import CompiledModel

Mode = Literal["with_extensions", "standard_only"]

CONSEQUENCES: Mapping[str, str] = {
    "additivity": "may aggregate a metric across an axis it must not be summed over",
    "non_additive_axes": "may sum a semi-additive metric over its non-additive axis",
    "aggregate_class": "may re-aggregate a holistic metric such as a distinct count",
    "components": "may average a ratio instead of recomputing it from its components",
    "zero_denominator": "may divide by zero differently",
    "rounding": "may round differently",
    "cardinality": "may join without knowing the relationship's cardinality, and fan out",
    "bridge": "may sum a many-to-many relationship without allocating it",
    "unit": "may combine incompatible units",
    "currency": "may combine incompatible currencies",
    "default_time_zone": "may bucket times in another zone",
    "time_zone": "may bucket times in another zone",
    "window": "may compute the window without its declared partitions and order",
}
GENERIC = "loses this declaration; the reader computes without it"


class ExportRefused(ValueError):
    """An export this caller or this target may not have."""


@dataclass(frozen=True, slots=True)
class Loss:
    pointer: str
    element: str
    declarations: tuple[str, ...]
    consequences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Export:
    content: bytes
    format: str
    losses: tuple[Loss, ...]


def _plain(value: object) -> Any:  # noqa: ANN401 - a JSON document
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _remove(document: dict[str, Any], pointers: Sequence[str]) -> None:
    """Remove the list items at `pointers` (`/…/custom_extensions/<n>`), last first."""
    for pointer in sorted(
        pointers, key=lambda p: [int(x) if x.isdigit() else x for x in p.split("/")], reverse=True
    ):
        *path, index = pointer.lstrip("/").split("/")
        node: Any = document
        for part in path:
            node = node[int(part)] if isinstance(node, list) else node[part]
        del node[int(index)]
    _drop_empty(document)


def _drop_empty(node: Any) -> None:  # noqa: ANN401
    if isinstance(node, dict):
        if node.get("custom_extensions") == []:
            del node["custom_extensions"]
        for value in node.values():
            _drop_empty(value)
    elif isinstance(node, list):
        for value in node:
            _drop_empty(value)


def export_model(compiled: CompiledModel, *, role: str, target_entry: str, mode: Mode) -> Export:
    """The model as an Ossie document at `target_entry`, and every loss."""
    if role != "builder":
        raise ExportRefused("only a builder exports the model")
    if target_entry != compiled.ossie.registry_entry:
        raise ExportRefused("a conversion to another registry entry is not available")
    model = compiled.ossie
    if mode == "with_extensions":
        return Export(model.content, model.format, ())
    if mode != "standard_only":
        raise ExportRefused(f"unknown mode {mode!r}")
    document = copy.deepcopy(_plain(model.document))
    losses = []
    for extension in sorted(model.product_extensions, key=lambda e: e.pointer):
        declarations = tuple(
            sorted(k for k in extension.payload if k not in {"version", "element"})
        )
        losses.append(
            Loss(
                pointer=extension.pointer.rsplit("/custom_extensions/", 1)[0],
                element=extension.element,
                declarations=declarations,
                consequences=tuple(CONSEQUENCES.get(k, GENERIC) for k in declarations),
            )
        )
    _remove(document, [e.pointer for e in model.product_extensions])
    if model.format == "json":
        content = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    else:
        content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
    return Export(content, model.format, tuple(losses))
