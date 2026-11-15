"""The transform contract's analysis values: the columns of the relations a model reads, asked
for and given back as data instead of a callback, the automatic filter's refusal, a
reconciliation check's staging and measure types, and a run's lineage inputs (#486 G5).

An operation that needs the columns of relations it reads answers `ColumnsNeeded` with every
relation it could not find among the `known` ones it was given; the caller describes them and
calls again with them added, until the operation answers. A relation that cannot be described
is given with `columns=None`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelationColumns:
    """The column names of one relation as the binder sees it (None: it cannot be described)."""

    relation: tuple[str, ...]
    columns: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ColumnsNeeded:
    """The relations an operation must know the columns of before it can answer."""

    relations: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the automatic filter cannot be proven, and where the time column was found (0-based
    offset in the SQL) and on which line, when it was."""

    reason: str
    location: int | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationStage:
    """A reconciliation check against a database source: its side's SQL, read on `database`,
    stands in as the empty table `schema.table` so the comparison binds as a run lands it."""

    database: str
    source_sql: str
    schema: str
    table: str


@dataclass(frozen=True, slots=True)
class MeasureDifference:
    """The type of a reconciliation check's measure difference, and the measure types that are
    not exact numbers (law 11: floats are never compared)."""

    kind: str
    inexact: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LineageScope:
    """What every lineage event of a project's runs shares."""

    producer: str  # a URI naming this release of the product's transform module
    job_namespace: str
    lake_namespace: str


@dataclass(frozen=True, slots=True)
class LineageRun:
    """One model's part of a run, as the runner saw it: `selected` are the columns its SQL
    binds to (column lineage), `output` those of the table as stored (the schema facet)."""

    model: str
    run_id: str  # this model run's UUID
    started_at: str
    finished_at: str
    outcome: str  # success or failed
    selected: tuple[tuple[str, str], ...]
    output: tuple[tuple[str, str], ...]
    rows_written: str | None
    error: str | None  # "<code>: <message>" of the failing model
