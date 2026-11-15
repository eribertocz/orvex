"""The transform contract's project values: a model, what discovery found and the dependency
graph, as they cross the contract (#486 G5: frozen dataclasses of plain values)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orvex.contracts.model_config.v1 import ModelConfig, ModelError

# The name of a model's schema and table (contract `model-config` v1).
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")

Columns = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Model:
    """One model: `schema.table`, its SQL as written, and its configuration (defaults applied)."""

    schema: str
    table: str
    sql_file: str  # relative to the project folder
    sql: str
    config: ModelConfig
    config_file: str | None
    # The sidecar's numbers as written, by JSON pointer (exact text for bounds and tolerances).
    numbers: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True, slots=True)
class Discovery:
    models: tuple[Model, ...]
    problems: tuple[ModelError, ...]


@dataclass(frozen=True, slots=True)
class Graph:
    """Models in run order (dependencies first, ties by name), and their direct dependencies."""

    order: tuple[str, ...]
    dependencies: dict[str, tuple[str, ...]]
    sources: dict[str, tuple[str, ...]]
    problems: tuple[ModelError, ...]
    # The database source (DuckDB, SQLite, PostgreSQL) each model reading one reads (#372):
    # such a model reads that connection alone, and is landed by its worker.
    lands: dict[str, str] = field(default_factory=dict)
