"""The transform contract's run steps: what a model's run executes in the store, in order, and
how its checks compare, as they cross the contract (#486 G5)."""

from __future__ import annotations

from dataclasses import dataclass

from orvex.contracts.store.v1 import Parameter, StatementRequest

# The `$since` parameter of an incremental model, and the store's parameter type for each
# temporal output type it is bound as.
SINCE = "since"
PARAMETER_TYPES = {
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}


@dataclass(frozen=True, slots=True)
class Check:
    """A value that must be zero; otherwise the run fails with `code`.

    With `detail`, the SQL returns `'0'` or a text describing what failed, which replaces
    `{detail}` in `message`. `parameters` are bound to the SQL (never formatted into it).
    """

    sql: str
    code: str
    message: str
    parameters: tuple[Parameter, ...] = ()
    detail: bool = False

    def failure(self, value: str) -> str:
        """The message for a failing value."""
        return self.message.replace("{detail}", value) if self.detail else self.message


@dataclass(frozen=True, slots=True)
class Count:
    """The number of loaded rows, recorded as rows read (and written)."""

    sql: str


@dataclass(frozen=True, slots=True)
class Refuse:
    """The model cannot run as declared; the run fails with `code` before writing anything."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Watermark:
    """The model's new watermark, as exact text: the larger of the previous one and the loaded
    rows' largest `time_column` (None when there is neither). The runner stores it with the
    statements of the `watermark_statements` operation, in the same transaction."""

    request: StatementRequest
    model: str
    reset: bool  # a full refresh: the previous watermark is dropped even if nothing loads


@dataclass(frozen=True, slots=True)
class Land:
    """Replace `schema.table` with what `sql` returns on the database source `source`, read in
    its own worker, never in the lake's session (#372): the one way a model reads a DuckDB,
    SQLite or PostgreSQL source."""

    source: str
    sql: str
    parameters: tuple[Parameter, ...]
    schema: str
    table: str


@dataclass(frozen=True, slots=True)
class ModelTest:
    """One test of a model: `request` counts the failing rows (0 passes)."""

    name: str
    request: StatementRequest


@dataclass(frozen=True, slots=True)
class Comparison:
    """How one reconciliation check compares, from the binder: the measures' common type, and
    the positions of its text groups (compared with the binary collation)."""

    kind: str
    text_groups: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Reconcile:
    """One reconciliation check: `request` counts the failing groups (0 passes). `land`: for a
    database source, its side landed first from its worker (#372), dropped after the check."""

    name: str
    request: StatementRequest
    land: Land | None = None


Step = StatementRequest | Check | Count | Refuse | Watermark | Land
PlannedStep = Step | ModelTest | Reconcile
