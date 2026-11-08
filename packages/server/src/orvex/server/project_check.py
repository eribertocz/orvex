"""`check_project`: load a project, discover and order its models, and bind them in a scratch
lake; every problem, nothing written.

The server composes the modules: the project loader, model discovery, the dependency graph and
the run plan (`transform`), and the lake (`store`). Transform never opens the lake.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orvex.contracts.model_config.v1 import Reconciliation
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    Parameter,
    ParameterType,
    StatementRequest,
)
from orvex.server import lake_separation
from orvex.server.error_problems import (
    _from_model_error,
    _load_problem,
    _store_message,
    _store_problem,
)
from orvex.server.identity import Identity, load_identity
from orvex.server.lake_open import _limits, lake_location
from orvex.server.lake_place import Place
from orvex.server.problems import Problem
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, open_lake
from orvex.transform import auto_filter, incremental, model_tests, reconciliation, schedule
from orvex.transform.graph import (
    Graph,
    build_graph,
    compiled_sql,
    known_output_names,
    test_relations,
)
from orvex.transform.incremental import Columns, since_bound_problem
from orvex.transform.models import Discovery, Model, discover_models
from orvex.transform.reconciliation import Comparison
from orvex.transform.sources import source_attachments

DATABASE_TYPES = frozenset({"duckdb", "sqlite", "postgres"})


@dataclass(frozen=True, slots=True)
class Checked:
    project: Project | None
    discovery: Discovery
    graph: Graph | None
    problems: tuple[Problem, ...]
    columns: Mapping[str, Columns] = field(default_factory=dict)
    # Each model's reconciliation checks: the type of their measure difference.
    differences: Mapping[str, list[Comparison]] = field(default_factory=dict)
    # Incremental models with `filter: auto`: their SQL with the proven filter (P1-13).
    filtered: Mapping[str, str] = field(default_factory=dict)
    # Where the project's lake is (#120); None when its configuration is refused.
    place: Place | None = None


def databases(project: Project) -> frozenset[str]:
    """The connections that carry SQL of their own, read only in a worker (#372)."""
    return frozenset(
        name
        for name, connection in project.connections.items()
        if connection.type in DATABASE_TYPES
    )


def check_project(
    folder: Path, identity: Identity | None = None, *, instance: Path | None = None
) -> Checked:
    """Load the project, discover its models and order them; every problem, nothing written.
    With the `instance` folder, existing lakes are also checked apart from every registered
    project's files, both ways (P2-30 PR 2)."""
    try:
        project = load_project(folder)
    except ProjectLoadError as error:
        return Checked(None, Discovery((), ()), None, (_load_problem(error),))
    slug = (identity or load_identity()).slug
    home = project.folder / f".{slug}"
    place: Place | None = None
    problems: list[Problem] = []
    try:
        place = lake_location(project, home)
    except ProjectLoadError as error:
        problems.append(_load_problem(error))
    if instance is not None:
        folders = lake_separation.registered(instance, project.folder)
        found = lake_separation.instance(instance, folders, slug)
        problems += lake_separation.problems(found, project.folder)
    discovery = discover_models(project.folder, project.sources)
    sources = set(project.sources)
    graph = build_graph(discovery.models, sources, databases(project), project.direct)
    problems += [_from_model_error(item) for item in (*discovery.problems, *graph.problems)]
    kinds = {name: connection.type for name, connection in project.sources.items()}
    zone = project.settings.time_zone
    for model in discovery.models:
        problems += [_from_model_error(item) for item in schedule.config_problems(model, zone)]
        problems += [_from_model_error(item) for item in incremental.config_problems(model)]
        problems += [_from_model_error(item) for item in reconciliation.problems(model, kinds)]
    columns: dict[str, Columns] = {}
    differences: dict[str, list[Comparison]] = {}
    filtered: dict[str, str] = {}
    # A repeated output name (#113): found from the text when it decides the names; otherwise
    # the model is bound, as below.
    unknown_names = False
    for model in discovery.models:
        names = known_output_names(model.sql)
        if names is None:
            unknown_names = True
        else:
            problems += _repeated_names(model, names)
    needs_binding = unknown_names or any(
        m.config.materialized == "incremental" or m.config.tests or m.config.reconciliation
        for m in discovery.models
    )
    if needs_binding and not problems:
        columns, differences, filtered, bind_problems = _bind(project, discovery, graph)
        problems += bind_problems
        problems += [
            _from_model_error(item) for item in incremental.problems_for(discovery.models, columns)
        ]
    return Checked(
        project,
        discovery,
        graph,
        tuple(problems),
        columns,
        differences,
        filtered,
        place,
    )


def _bind(
    project: Project, discovery: Discovery, graph: Graph
) -> tuple[
    dict[str, Columns],
    dict[str, list[Comparison]],
    dict[str, str],
    list[Problem],
]:
    """Output columns of every model, from DuckDB's binder; nothing is executed.

    Binding happens in a scratch lake in a temporary folder (never the project's): upstream
    models are stand-in empty tables with the columns found for them, and sources are attached
    as for a run.
    """
    by_name = {model.name: model for model in discovery.models}
    relations = test_relations(discovery.models)
    known = databases(project)
    columns: dict[str, Columns] = {}
    differences: dict[str, list[Comparison]] = {}
    filtered: dict[str, str] = {}
    problems: list[Problem] = []
    failed: set[str] = set()
    scratch = Path(tempfile.mkdtemp(prefix="bind-"))
    try:
        location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(scratch))
        try:
            lake = open_lake(location, _limits(), source_attachments(project.sources))
        except StoreError as error:
            return {}, {}, {}, [_store_problem(error, ".", "")]
        try:
            for name in graph.order:
                model = by_name[name]
                if failed & set(graph.dependencies.get(name, ())):
                    failed.add(name)
                    continue
                try:
                    output = _describe(
                        lake, model, compiled_sql(model, by_name), graph.lands.get(name)
                    )
                except StoreError as error:
                    failed.add(name)
                    problems.append(
                        Problem(
                            "INVALID_MODEL_SQL",
                            model.sql_file,
                            None,
                            f"{name}: {_store_message(error)}",
                        )
                    )
                    continue
                repeated = _repeated_names(model, [column for column, _ in output])
                if repeated:
                    failed.add(name)
                    problems += repeated
                    continue
                columns[name] = output
                # A computed time column bounded by its alias: only now are the tables'
                # columns known (#162).
                bounded = since_bound_problem(model, lambda parts: _columns_of(lake, parts, known))
                if bounded is not None:
                    failed.add(name)
                    problems.append(_from_model_error(bounded))
                    continue
                if _automatic(model):
                    proven = _prove_filter(lake, model, output, by_name, graph.lands.get(name))
                    if isinstance(proven, Problem):
                        problems.append(proven)
                    else:
                        filtered[name] = proven
                if model.config.materialized != "view":
                    _stand_in(lake, model, output)
                # Tests and reconciliation check the stored table, whatever produced it.
                if model.config.tests:
                    problems += _bind_tests(lake, model, columns, by_name, relations)
                if model.config.reconciliation:
                    found, kinds = _bind_reconciliation(lake, model, relations[name], known)
                    problems += found
                    differences[name] = kinds
        finally:
            lake.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return columns, differences, filtered, problems


def _repeated_names(model: Model, names: list[str]) -> list[Problem]:
    """One problem per output column name the model repeats, in output order (#113).

    Names are compared as DuckDB does, without letter case: a table cannot hold `a` and `A`,
    and `CREATE TABLE … AS` would silently store the second as `A_1`."""
    spellings: dict[str, list[str]] = {}
    for name in names:
        spellings.setdefault(name.casefold(), []).append(name)
    return [
        Problem(
            "INVALID_MODEL_SQL",
            model.sql_file,
            None,
            f"{model.name}: the output repeats the column name {found[0]} "
            f"({', '.join(found)}); column names are not case-sensitive.",
        )
        for found in spellings.values()
        if len(found) > 1
    ]


def _columns_of(
    lake: Lake, parts: tuple[str, ...], known: frozenset[str] = frozenset()
) -> frozenset[str] | None:
    """The lowercase column names of a table or source table, as the binder sees it in the
    scratch lake (a database source's in its worker, #372); None when it cannot be described."""
    found = _relation_columns(lake, list(parts), known)
    return None if found is None else frozenset(name.lower() for name in found)


def _relation_columns(lake: Lake, parts: list[str], known: frozenset[str]) -> list[str] | None:
    quoted = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
    sql = f"SELECT * FROM {quoted}"
    try:
        if parts and parts[0].lower() in known:
            return [name for name, _ in lake.describe_source(parts[0].lower(), sql)]
        return [name for name, _ in lake.describe(sql)]
    except StoreError:
        return None


def _automatic(model: Model) -> bool:
    incremental_config = model.config.incremental
    return (
        model.config.materialized == "incremental"
        and incremental_config is not None
        and (incremental_config.filter or "auto") == "auto"
    )


def _prove_filter(
    lake: Lake,
    model: Model,
    output: Columns,
    by_name: Mapping[str, Model],
    land: str | None = None,
) -> str | Problem:
    """The model's SQL with the proven `> $since` filter, bound once to check it; or why the
    filter cannot be proven (`INCREMENTAL_FILTER_NOT_PROVABLE`, with the time column's line)."""
    incremental_config = model.config.incremental
    assert incremental_config is not None
    described: dict[tuple[str, ...], list[str] | None] = {}

    def columns_of(path: list[str]) -> list[str] | None:
        parts = tuple(path)
        if parts not in described:
            described[parts] = _relation_columns(
                lake, list(parts), frozenset({land}) if land else frozenset()
            )
        return described[parts]

    time_column = incremental_config.time_column.root
    result = auto_filter.filtered_sql(model.sql, time_column, columns_of)
    if isinstance(result, auto_filter.Refusal):
        return Problem(
            "INCREMENTAL_FILTER_NOT_PROVABLE",
            model.sql_file,
            auto_filter.line_of(model.sql, result.location),
            f"{model.name}: a filter on {time_column} cannot be proven safe "
            f"(reason: {result.reason}); set filter: manual and place $since in the SQL.",
        )
    try:
        rewritten = _describe(lake, model, compiled_sql(model, by_name, result), land)
    except StoreError as error:
        return Problem(
            "INCREMENTAL_FILTER_NOT_PROVABLE",
            model.sql_file,
            None,
            f"{model.name}: a filter on {time_column} cannot be proven safe "
            f"(reason: rebind, the filtered SQL does not bind: {_store_message(error)}); set "
            "filter: manual and place $since in the SQL.",
        )
    if [name for name, _ in rewritten] != [name for name, _ in output]:
        return Problem(
            "INCREMENTAL_FILTER_NOT_PROVABLE",
            model.sql_file,
            None,
            f"{model.name}: a filter on {time_column} cannot be proven safe "
            "(reason: rebind, the filtered SQL changes the output columns); set filter: manual and "
            "place $since in the SQL.",
        )
    return result


def _bind_reconciliation(
    lake: Lake, model: Model, relation: str, known: frozenset[str] = frozenset()
) -> tuple[list[Problem], list[Comparison]]:
    """Each check bound on the model and its source: the type of its measure difference, which
    must be an exact number (integers or DECIMAL, law 11). A database source's side is bound in
    its worker and stood in by an empty table of its columns, as a run lands it (#372)."""
    file = model.config_file or model.sql_file
    problems: list[Problem] = []
    kinds: list[Comparison] = []
    for index, check in enumerate(model.config.reconciliation or []):
        pointer = f"/reconciliation/{index}"
        try:
            staged = _stage_reconciliation(lake, model, index, check, known)
            described = lake.describe(reconciliation.measures_sql(check, relation, staged))
        except StoreError as error:
            problems.append(
                Problem(
                    "MODEL_CONFIG_INVALID",
                    file,
                    None,
                    f"{file}: {pointer}: {check.name} cannot run: {_store_message(error)}",
                )
            )
            continue
        mismatched = [
            position
            for position in range(len(check.group_by or []))
            if described[2 + 2 * position][1] != described[3 + 2 * position][1]
        ]
        if mismatched:
            # Different key types would be converted implicitly, and two model groups could
            # match one source group: the comparison would no longer be one-to-one.
            position = mismatched[0]
            problems.append(
                Problem(
                    "MODEL_CONFIG_INVALID",
                    file,
                    None,
                    f"{file}: {pointer}/group_by/{position}: {check.name} groups a "
                    f"{described[2 + 2 * position][1]} in the model and a "
                    f"{described[3 + 2 * position][1]} in the source; cast one side so both "
                    "have the same type.",
                )
            )
            continue
        text_groups = frozenset(
            position
            for position in range(len(check.group_by or []))
            if described[2 + 2 * position][1] == "VARCHAR"
        )
        described = described[:2]
        kind = reconciliation.common_type(described[0][1], described[1][1])
        inexact = [typ for _, typ in described if not reconciliation.exact_difference(typ)]
        if inexact:
            problems.append(
                Problem(
                    "MODEL_CONFIG_INVALID",
                    file,
                    None,
                    f"{file}: {pointer}/measure: {check.name} compares {inexact[0]} values; use a "
                    "measure with an exact type (integer or DECIMAL) — floats are not "
                    "compared (law 11).",
                )
            )
            continue
        kinds.append(Comparison(kind, text_groups))
    return problems, kinds


def _stage_reconciliation(
    lake: Lake, model: Model, index: int, check: Reconciliation, known: frozenset[str]
) -> str | None:
    """For a check against a database source: an empty lake table with the columns its side
    lands with, so the comparison binds as a run will run it; None for any other source."""
    database = reconciliation.database_of(check, known)
    if database is None:
        return None
    output = lake.describe_source(database, reconciliation.source_side(check))
    name = reconciliation.staged_name(model, index)
    quoted = ", ".join(
        f'"{column.replace(chr(34), chr(34) * 2)}" {kind}' for column, kind in output
    )
    staged = f'"{reconciliation.STAGE_SCHEMA}"."{name}"'
    with lake.transaction() as transaction:
        for sql in (
            f'CREATE SCHEMA IF NOT EXISTS "{reconciliation.STAGE_SCHEMA}"',
            f"CREATE OR REPLACE TABLE {staged} ({quoted})",
        ):
            transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
    return staged


def _bind_tests(
    lake: Lake,
    model: Model,
    columns: Mapping[str, Columns],
    by_name: Mapping[str, Model],
    relations: Mapping[str, str],
) -> list[Problem]:
    """Problems of a model's tests: the rules on its columns, then each test query bound."""
    found = model_tests.problems(model, columns[model.name], columns, by_name)
    if found:
        return [_from_model_error(item) for item in found]
    problems: list[Problem] = []
    file = model.config_file or model.sql_file
    tests = model_tests.steps(model, relations[model.name], columns[model.name], relations)
    for index, test in enumerate(tests):
        try:
            lake.describe(test.request.sql, test.request.parameters)
        except StoreError as error:
            problems.append(
                Problem(
                    "MODEL_CONFIG_INVALID",
                    file,
                    None,
                    f"{file}: /tests/{index}: {test.name} cannot run: {_store_message(error)}",
                )
            )
    return problems


def _describe(lake: Lake, model: Model, sql: str, land: str | None = None) -> Columns:
    """Output columns of a model's SQL; for a model reading a database source (`land`), as its
    worker binds them and they land (#372). An incremental model's `$since` is bound as a NULL
    of each temporal type in turn; the binding whose `time_column` has that type is the one a
    run will use (any other model has no parameter to bind)."""

    def described(text: str, parameters: Sequence[Parameter] = ()) -> Columns:
        if land is not None:
            return lake.describe_source(land, text, parameters)
        return lake.describe(text, parameters)

    incremental_config = model.config.incremental
    if model.config.materialized != "incremental" or incremental_config is None:
        return described(sql)
    if not incremental.uses_since(sql):
        return described(sql)
    time_column = incremental_config.time_column.root.lower()
    first: Columns | None = None
    error: StoreError | None = None
    for output_type, parameter_type in incremental.PARAMETER_TYPES.items():
        since = Parameter(name=incremental.SINCE, type=ParameterType(parameter_type), value=None)
        try:
            output = described(sql, [since])
        except StoreError as failed:
            error = error or failed
            continue
        if {name.lower(): kind for name, kind in output}.get(time_column) == output_type:
            return output
        first = first or output
    if first is not None:
        return first  # the time column is not temporal: the column rules report it
    assert error is not None
    raise error


def _stand_in(lake: Lake, model: Model, output: Columns) -> None:
    """An empty table with the model's columns, so models reading it can be bound."""
    quoted = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}" {kind}' for name, kind in output)
    with lake.transaction() as transaction:
        transaction.execute(
            StatementRequest(
                kind="statement",
                sql=f'CREATE SCHEMA IF NOT EXISTS "{model.schema}"',
                parameters=[],
            )
        )
        transaction.execute(
            StatementRequest(
                kind="statement",
                sql=f'CREATE TABLE "{model.schema}"."{model.table}" ({quoted})',
                parameters=[],
            )
        )
