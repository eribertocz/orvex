"""The model's relationship graph, as C1 reads it (P2-11, #310 §2).

A relationship's `from` is its many side (`ossie-extensions` v1), so a step from `from` to `to`
never multiplies rows. The reverse step does, unless the relationship is `one_to_one`. A path
crosses each dataset at most once. The number of paths searched is bounded: past the bound,
the search is refused, never cut short with a partial answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql

MAX_PATHS = 256
MAX_STEPS = 10_000  # partial paths explored, whatever their end


@dataclass(frozen=True, slots=True)
class Step:
    relationship: str  # normalized
    to: str  # the dataset this step reaches, normalized
    safe: bool  # never multiplies the rows it starts from
    temporal: bool  # an as-of join (C6)


@dataclass(frozen=True, slots=True)
class Path:
    steps: tuple[Step, ...]

    @property
    def relationships(self) -> tuple[str, ...]:
        return tuple(step.relationship for step in self.steps)

    @property
    def datasets(self) -> tuple[str, ...]:
        return tuple(step.to for step in self.steps)


def _items(node: object, key: str) -> list[Any]:
    value = node.get(key, ()) if isinstance(node, Mapping) else ()
    return list(value) if isinstance(value, list | tuple) else []


class Graph:
    def __init__(self, compiled: CompiledModel) -> None:
        document = compiled.ossie.document
        payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in compiled.ossie.product_extensions
        }
        self.names: dict[str, str] = {}  # normalized -> as written
        self.payloads: dict[str, Mapping[str, Any]] = {}
        self.keys: dict[str, tuple[str, ...]] = {}
        self.written: dict[tuple[str, str], str] = {}  # (dataset, column) -> as written
        for at, dataset in enumerate(_items(document, "datasets")):
            name = sql.normalize(str(dataset["name"]))
            self.names[name] = str(dataset["name"])
            self.payloads[name] = payloads.get(f"/datasets/{at}", {})
            self.keys[name] = tuple(sql.normalize(str(k)) for k in _items(dataset, "primary_key"))
            declared = self.payloads[name].get("grain")
            dimensions = _items(declared, "dimensions") if isinstance(declared, Mapping) else []
            for column in [*_items(dataset, "primary_key"), *dimensions]:
                self.written.setdefault((name, sql.normalize(str(column))), str(column))
        self.relationship_names: dict[str, str] = {}
        self.links: dict[str, list[Step]] = {name: [] for name in self.names}
        for at, relationship in enumerate(_items(document, "relationships")):
            name = sql.normalize(str(relationship["name"]))
            self.relationship_names[name] = str(relationship["name"])
            payload = payloads.get(f"/relationships/{at}", {})
            one_to_one = payload.get("cardinality") == "one_to_one"
            temporal = "temporal" in payload
            left = sql.normalize(str(relationship["from"]))
            right = sql.normalize(str(relationship["to"]))
            self.links[left].append(Step(name, right, True, temporal))
            if left != right:  # a self-relationship is walked from the many side only
                self.links[right].append(Step(name, left, one_to_one, temporal))

    def bridge(self, dataset: str) -> bool:
        return "bridge" in self.payloads.get(dataset, {})

    def grain(self, dataset: str) -> tuple[str, ...]:
        """The dataset's natural grain: its declared `grain`, or its primary key."""
        declared = self.payloads.get(dataset, {}).get("grain")
        if isinstance(declared, Mapping):
            return tuple(sql.normalize(str(d)) for d in _items(declared, "dimensions"))
        return self.keys.get(dataset, ())

    def paths(self, start: str, end: str) -> list[Path]:
        """Every path from `start` to `end`, in a fixed order. A path is the sequence of
        relationships it crosses: it crosses each dataset once, except through a
        self-relationship, which it crosses at most once, so a cycle is never walked. From a
        dataset to itself, the empty path comes first."""
        found: list[Path] = [Path(())] if start == end else []
        pending: list[tuple[str, tuple[Step, ...], frozenset[str]]] = [
            (start, (), frozenset({start}))
        ]
        explored = 0
        while pending:
            at, steps, seen = pending.pop()
            explored += 1
            if explored > MAX_STEPS:
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "path_search"})
            for step in sorted(self.links.get(at, ()), key=lambda s: (s.relationship, s.to)):
                itself = step.to == at and step.relationship not in {s.relationship for s in steps}
                if step.to in seen and not itself:
                    continue
                route = (*steps, step)
                if step.to == end:
                    found.append(Path(route))
                    if len(found) > MAX_PATHS:
                        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "path_search"})
                    continue
                if not itself:
                    pending.append((step.to, route, seen | {step.to}))
        return sorted(found, key=lambda p: p.relationships)
