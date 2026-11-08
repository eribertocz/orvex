"""Leftover tables (#88): the lake tables no model stores now, and the statements that drop
them."""

from __future__ import annotations

from collections.abc import Sequence

from orvex.contracts.store.v1 import Parameter, ParameterType, StatementRequest
from orvex.store.lake import Lake
from orvex.transform.incremental import WATERMARKS
from orvex.transform.models import NAME, Model


def _leftovers(lake: Lake, models: Sequence[Model]) -> list[tuple[str, str]]:
    """Tables of the lake that no model stores now (removed, renamed, or now a `view`), in
    schemas a model could use: never `_meta` or `_stage` (#88). Only names a model could have
    are listed, so a table made outside a run with any other name is never dropped."""
    stored = {
        (model.schema.lower(), model.table.lower())
        for model in models
        if model.config.materialized != "view"
    }
    return sorted(
        (schema, table)
        for schema, table in lake.tables()
        if NAME.fullmatch(schema) and NAME.fullmatch(table) and (schema, table) not in stored
    )


def _drop_statements(leftovers: Sequence[tuple[str, str]]) -> list[StatementRequest]:
    """Drop each leftover and forget its watermark (a model added back later starts over)."""
    if not leftovers:
        return []
    # Both names match the model-name pattern (`_leftovers`): plain to quote.
    statements = [
        StatementRequest(kind="statement", sql=f'DROP TABLE "{schema}"."{table}"', parameters=[])
        for schema, table in leftovers
    ]
    statements += [
        StatementRequest(kind="statement", sql="CREATE SCHEMA IF NOT EXISTS _meta", parameters=[]),
        StatementRequest(
            kind="statement",
            sql=f"CREATE TABLE IF NOT EXISTS {WATERMARKS} (model VARCHAR, watermark VARCHAR)",
            parameters=[],
        ),
    ]
    for schema, table in leftovers:
        model = Parameter(name="p_model", type=ParameterType("VARCHAR"), value=f"{schema}.{table}")
        statements.append(
            StatementRequest(
                kind="statement",
                sql=f"DELETE FROM {WATERMARKS} WHERE model = $p_model",
                parameters=[model],
            )
        )
    return statements
