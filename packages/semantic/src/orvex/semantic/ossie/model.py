"""`OssieModel`: one Ossie document, read, validated and reference-resolved (P2-03).

Internal to `semantic`; P2-04 compiles it into the version-independent `CompiledModel`. It is
immutable: mappings are read-only views and sequences are tuples, all the way down.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from orvex.semantic.ossie.documents import Format

ElementKind = Literal["model", "dataset", "field", "metric", "relationship"]


def freeze(value: object) -> object:
    """A deeply immutable copy of plain parsed values."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ProductExtension:
    """A product entry (the current vendor name or a previous one), decoded and checked."""

    pointer: str
    element: ElementKind
    vendor_name: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ForeignExtension:
    """Another vendor's entry: preserved, listed, never interpreted (ADR-0030 §3)."""

    pointer: str
    vendor_name: str
    data_sha256: str


@dataclass(frozen=True)
class SelectedExpression:
    """The expression chosen for a field or metric (step 7), recorded for P2-04."""

    element: str  # pointer to the field or metric
    pointer: str  # pointer to the chosen text
    dialect: str
    text: str


AdvisoryKind = Literal[
    "to_columns_cover_no_key",
    "duplicate_dialect",
    "undeclared_product_dialect",
    "no_executable_expression",
]


@dataclass(frozen=True)
class Advisory:
    """A finding on a document that conforms to Ossie but that the product cannot yet compile or
    execute safely as written (Owner decision of 2026-11-01: conformance and executability are
    separate). The document loads; an operation that needs the element is refused with a typed
    error by the stage that runs it (P2-04 compilation, P2-15 and P2-20 relationships)."""

    pointer: str
    kind: AdvisoryKind


@dataclass(frozen=True)
class OssieModel:
    path: str = field(compare=False)
    format: Format = field(compare=False)
    content: bytes = field(compare=False, repr=False)
    identity: str = field(compare=False)  # SHA-256 of `content`: the document identity
    registry_entry: str
    registry_state: str
    reader: str
    capabilities: int
    document: Mapping[str, object] = field(repr=False)
    product_extensions: tuple[ProductExtension, ...]
    foreign_extensions: tuple[ForeignExtension, ...]
    expressions: tuple[SelectedExpression, ...]
    advisories: tuple[Advisory, ...]

    @property
    def preservation_only(self) -> bool:
        """Read by a `read_only` entry: kept and validated, never compiled (P2-04 refuses)."""
        return self.registry_state not in ("stable", "experimental")
