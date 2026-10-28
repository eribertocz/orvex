"""OpenLineage run events for every model run (P1-18).

Events follow the OpenLineage 2-0-2 specification. Datasets come from each model's SQL (DuckDB's
parser): the sources it reads, the models it reads, and the lake table it writes. Column lineage
is given only when the parse tree proves it — a single `SELECT` over tables, without `*`,
subqueries or set operations, whose column references are unambiguous — and is never guessed.
No secret is ever part of an event: connection namespaces carry hosts, databases and paths only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.transform.graph import LAKE_CATALOG, _cte_names, _parser, _path, _tree, _walk
from orvex.transform.models import Model

type Connection = (
    ParquetConnection
    | CsvConnection
    | DuckdbConnection
    | SqliteConnection
    | PostgresConnection
    | ClickhouseConnection
)
type Event = dict[str, Any]

SPEC = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
FACETS = {
    "parent": "https://openlineage.io/spec/facets/1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet",
    "errorMessage": (
        "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json"
        "#/$defs/ErrorMessageRunFacet"
    ),
    "schema": (
        "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
    ),
    "columnLineage": (
        "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json"
        "#/$defs/ColumnLineageDatasetFacet"
    ),
    "outputStatistics": (
        "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json"
        "#/$defs/OutputStatisticsOutputDatasetFacet"
    ),
}


@dataclass(frozen=True, slots=True)
class Dataset:
    namespace: str
    name: str


@dataclass(frozen=True, slots=True)
class Scope:
    """What every event of a project's runs shares."""

    producer: str  # a URI naming this release of the product's transform module
    job_namespace: str
    lake_namespace: str


def project_key(name: str) -> str:
    """The project's display name as a namespace part: lowercase, `-` between words."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def source_dataset(connection: Connection, parts: Sequence[str]) -> Dataset:
    """A source table as an OpenLineage dataset (naming conventions of the specification)."""
    if isinstance(connection, ParquetConnection | CsvConnection):
        return Dataset("file", str(connection.path.root))
    if isinstance(connection, PostgresConnection):
        schema = parts[0] if len(parts) > 1 else (connection.schema_ or "public")
        return Dataset(
            f"postgres://{connection.host}:{connection.port}",
            f"{connection.database}.{schema}.{parts[-1]}",
        )
    if isinstance(connection, ClickhouseConnection):
        return Dataset(
            f"clickhouse://{connection.host}:{connection.port}",
            f"{connection.database}.{parts[-1]}",
        )
    kind = "duckdb" if isinstance(connection, DuckdbConnection) else "sqlite"
    return Dataset(f"{kind}://{connection.path.root}", ".".join(parts))


def _relation(
    parts: list[str],
    ctes: set[str],
    models: Mapping[str, Model],
    connections: Mapping[str, Connection],
    lake: str,
) -> Dataset | None:
    if len(parts) == 3 and parts[0] == LAKE_CATALOG:
        parts = parts[1:]
    if len(parts) == 1 and parts[0] in ctes:
        return None
    if len(parts) >= 2 and parts[0] in connections:
        return source_dataset(connections[parts[0]], parts[1:])
    name = ".".join(parts)
    return Dataset(lake, name) if name in models else None


def inputs(
    model: Model, models: Mapping[str, Model], connections: Mapping[str, Connection], lake: str
) -> list[Dataset]:
    """The datasets a model reads, in order of first appearance, without repeats."""
    if model.extraction:
        return []  # its source query is described when extraction runs (P1-17)
    connection_ = _parser()
    try:
        tree = _tree(connection_, model.sql)
    finally:
        connection_.close()
    ctes = _cte_names(tree)
    found: list[Dataset] = []
    for node in _walk(tree):
        if node.get("type") != "BASE_TABLE":
            continue
        dataset = _relation(_path(node), ctes, models, connections, lake)
        if dataset is not None and dataset not in found:
            found.append(dataset)
    return found


def _aggregates() -> frozenset[str]:
    connection = _parser()
    try:
        rows = connection.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            "WHERE function_type = 'aggregate'"
        ).fetchall()
    finally:
        connection.close()
    return frozenset(str(row[0]) for row in rows)


def column_lineage(
    model: Model,
    output: Sequence[tuple[str, str]],
    models: Mapping[str, Model],
    connections: Mapping[str, Connection],
    lake: str,
    columns_of: Callable[[list[str]], Collection[str] | None] = lambda _: None,
) -> dict[str, list[dict[str, Any]]]:
    """Output column → the input fields it is computed from, when the parse tree proves it.

    `columns_of` gives the column names of a relation named by its path (`["archive", "data"]`,
    `["staging", "readings"]`), or None when unknown: an unqualified name counts only when it
    is a real column of the single table read, never a lateral alias or a lambda parameter.
    """
    if model.extraction:
        return {}
    connection = _parser()
    try:
        tree = _tree(connection, model.sql)
    finally:
        connection.close()
    statements = tree.get("statements") or []
    if len(statements) != 1:
        return {}
    node = statements[0]["node"]
    if node.get("type") != "SELECT_NODE" or _cte_names(tree):
        return {}
    tables: dict[str, Dataset] = {}
    paths: dict[str, list[str]] = {}
    pending = [node.get("from_table") or {}]
    while pending:
        part = pending.pop()
        if part.get("type") == "JOIN":
            pending += [part.get("left") or {}, part.get("right") or {}]
        elif part.get("type") == "BASE_TABLE":
            dataset = _relation(_path(part), set(), models, connections, lake)
            if dataset is None:
                return {}
            alias = str(part.get("alias") or part.get("table_name")).lower()
            tables[alias] = dataset
            paths[alias] = _path(part)
        else:
            return {}  # a subquery, a table function or no table: not provable
    items = node.get("select_list") or []
    if len(items) != len(output) or any(
        sub.get("class") in {"STAR", "SUBQUERY", "WINDOW", "LAMBDA"}
        for item in items
        for sub in _walk(item)
    ):
        return {}
    aggregates = _aggregates()
    lineage: dict[str, list[dict[str, Any]]] = {}
    for item, (column, _) in zip(items, output, strict=True):
        fields: list[dict[str, Any]] = []
        provable = True
        if item.get("class") == "COLUMN_REF":
            subtype = "IDENTITY"
        elif any(
            sub.get("class") in {"FUNCTION", "AGGREGATE"}
            and str(sub.get("function_name", "")).lower() in aggregates
            for sub in _walk(item)
        ):
            subtype = "AGGREGATION"
        else:
            subtype = "TRANSFORMATION"
        for sub in _walk(item):
            if sub.get("class") != "COLUMN_REF":
                continue
            names = [str(name) for name in sub.get("column_names", [])]
            if len(names) == 2 and names[0].lower() in tables:
                dataset = tables[names[0].lower()]
            elif len(names) == 1 and len(tables) == 1:
                ((alias, dataset),) = tables.items()
                known = columns_of(paths[alias])
                if known is None or names[0].lower() not in {c.lower() for c in known}:
                    provable = False  # a lateral alias, or a column not known to be the table's
                    break
            else:
                provable = False
                break
            field = {
                "namespace": dataset.namespace,
                "name": dataset.name,
                "field": names[-1],
                "transformations": [{"type": "DIRECT", "subtype": subtype}],
            }
            if field not in fields:
                fields.append(field)
        if provable and fields:
            lineage[column] = fields
    return lineage


def _facet(scope: Scope, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    return {"_producer": scope.producer, "_schemaURL": FACETS[kind], **body}


@dataclass(frozen=True, slots=True)
class ModelRun:
    """One model's part of a run, as the runner saw it."""

    model: Model
    run_id: str  # this model run's UUID
    started_at: str
    finished_at: str
    outcome: str  # success or failed
    output: Sequence[tuple[str, str]]
    inputs: Sequence[Dataset]
    column_lineage: Mapping[str, list[dict[str, Any]]]
    rows_written: str | None
    error: str | None  # "<code>: <message>" of the failing model


def events(scope: Scope, parent_run_id: str, runs: Sequence[ModelRun]) -> list[Event]:
    """START and COMPLETE (or FAIL) for each model that started, in run order."""
    found: list[Event] = []
    for item in runs:
        output: dict[str, Any] = {
            "namespace": scope.lake_namespace,
            "name": item.model.name,
            "facets": {
                "schema": _facet(
                    scope,
                    "schema",
                    {
                        "fields": [
                            {"name": name, "type": kind, "ordinal_position": position}
                            for position, (name, kind) in enumerate(item.output, 1)
                        ]
                    },
                )
            },
        }
        if item.column_lineage:
            output["facets"]["columnLineage"] = _facet(
                scope,
                "columnLineage",
                {"fields": {name: {"inputFields": f} for name, f in item.column_lineage.items()}},
            )
        run_facets: dict[str, Any] = {
            "parent": _facet(
                scope,
                "parent",
                {
                    "run": {"runId": parent_run_id},
                    "job": {"namespace": scope.job_namespace, "name": "run"},
                },
            )
        }
        datasets = [{"namespace": d.namespace, "name": d.name} for d in item.inputs]
        job = {"namespace": scope.job_namespace, "name": item.model.name}
        base = {"producer": scope.producer, "schemaURL": SPEC, "job": job, "inputs": datasets}
        found.append(
            {
                **base,
                "eventType": "START",
                "eventTime": item.started_at,
                "run": {"runId": item.run_id, "facets": run_facets},
                "outputs": [output],
            }
        )
        finished_facets = dict(run_facets)
        finished_output = {**output, "facets": dict(output["facets"])}
        if item.outcome == "success":
            if item.rows_written is not None:
                finished_output["outputFacets"] = {
                    "outputStatistics": _facet(
                        scope, "outputStatistics", {"rowCount": int(item.rows_written)}
                    )
                }
        elif item.error is not None:
            finished_facets["errorMessage"] = _facet(
                scope, "errorMessage", {"message": item.error, "programmingLanguage": "SQL"}
            )
        found.append(
            {
                **base,
                "eventType": "COMPLETE" if item.outcome == "success" else "FAIL",
                "eventTime": item.finished_at,
                "run": {"runId": item.run_id, "facets": finished_facets},
                "outputs": [finished_output],
            }
        )
    return found
