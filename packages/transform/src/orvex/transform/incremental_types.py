"""The vocabulary of incremental runs: shared names, column lists and SQL helpers (the run steps
and `$since` are the transform contract's, `orvex.contracts.transform.steps`)."""

from __future__ import annotations

from collections.abc import Sequence

from orvex.contracts.store.v1 import Parameter, StatementRequest
from orvex.contracts.transform.models import Model

STAGE_SCHEMA = "_stage"
TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"})
WATERMARKS = "_meta.watermarks"
LOWEST = "-infinity"
KEYED_STRATEGIES = frozenset({"merge", "delete_insert"})

type Columns = Sequence[tuple[str, str]]


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def statement_request(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def target_table(model: Model) -> str:
    return f"{quote_identifier(model.schema)}.{quote_identifier(model.table)}"
