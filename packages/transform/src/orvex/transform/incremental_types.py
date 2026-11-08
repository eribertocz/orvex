"""The vocabulary of incremental runs: shared names, column lists and the kinds of run step."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from orvex.contracts.store.v1 import Parameter, StatementRequest
from orvex.transform.models import Model

STAGE_SCHEMA = "_stage"
TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"})
SINCE = "since"
WATERMARKS = "_meta.watermarks"
LOWEST = "-infinity"
# The store's parameter type for each temporal output type.
PARAMETER_TYPES = {
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}
KEYED_STRATEGIES = frozenset({"merge", "delete_insert"})

type Columns = Sequence[tuple[str, str]]


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
    rows' largest `time_column` (None when there is neither). The runner stores it with
    `watermark_statements`, in the same transaction."""

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


type Step = StatementRequest | Check | Count | Refuse | Watermark | Land


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def statement_request(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def target_table(model: Model) -> str:
    return f"{quote_identifier(model.schema)}.{quote_identifier(model.table)}"
