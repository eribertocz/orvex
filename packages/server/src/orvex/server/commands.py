"""`validate` and `run`: check a project and build its models into the local lake (P1-09).

The server composes the modules: the project loader (here), model discovery, the dependency
graph and the run plan (`transform`), and the lake (`store`). Transform never opens the lake;
it hands the server a list of statements that run in one write transaction.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

from orvex.contracts.model_config.v1 import ModelError, Reconciliation
from orvex.contracts.run_record.v1 import (
    CheckResult,
    ModuleVersion,
    RowsRead,
    RowsWritten,
    RunError,
    RunRecord,
    Versions,
)
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    Parameter,
    ParameterType,
    ReadRequest,
    StatementRequest,
)
from orvex.semantic.ossie import sql
from orvex.server import evidence_runs, heartbeat, lake_place, lake_separation, semantic_model
from orvex.server.identity import Identity, load_identity
from orvex.server.lake_place import Place
from orvex.server.problems import Problem
from orvex.server.project import (
    Project,
    ProjectLoadError,
    load_project,
    resolve_secret,
)
from orvex.server.versions import module_versions
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, Transaction, open_lake
from orvex.transform import (
    auto_filter,
    incremental,
    lineage,
    model_tests,
    reconciliation,
    schedule,
)
from orvex.transform.graph import (
    Graph,
    build_graph,
    compiled_sql,
    known_output_names,
    plan_run,
    test_relations,
)
from orvex.transform.incremental import (
    WATERMARKS,
    Columns,
    Count,
    Land,
    Refuse,
    Step,
    Watermark,
    since_bound_problem,
)
from orvex.transform.model_tests import ModelTest
from orvex.transform.models import NAME, Discovery, Model, discover_models
from orvex.transform.reconciliation import Comparison, Reconcile
from orvex.transform.run_records import committed_request, history_request, record_statements
from orvex.transform.sources import Attachment, source_attachments

LOCK_FILE = "lake.lock"
DEFAULT_MEMORY = "4GB"
DEFAULT_TIMEOUT_MS = 3_600_000
GIT_TIMEOUT_S = 10


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


class ProjectLocked(Exception):
    """Another process holds the project's local lake."""


def _from_model_error(error: ModelError) -> Problem:
    details = error.details
    return Problem(error.code.root, details.file, details.line, error.message)


def _load_problem(error: ProjectLoadError) -> Problem:
    details = error.error.details
    return Problem(error.code, details.file or ".", details.line, error.error.message)


DATABASE_TYPES = frozenset({"duckdb", "sqlite", "postgres"})


def databases(project: Project) -> frozenset[str]:
    """The connections that carry SQL of their own, read only in a worker (#372)."""
    return frozenset(
        name
        for name, connection in project.connections.items()
        if connection.type in DATABASE_TYPES
    )


def lake_location(project: Project, home: Path) -> Place:
    """Where the project's lake is: the configured catalog, checked against the one the
    existing lake was created with (#120). Nothing is opened or created."""
    chosen = lake_place.place(project, home)
    lake_place.check_unchanged(home, chosen)
    return chosen


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


def memory_limit() -> str:
    """Three quarters of the machine's memory, or 4GB when it cannot be read."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except AttributeError, ValueError, OSError:
        return DEFAULT_MEMORY
    return f"{max(1, total * 3 // 4 // (1024 * 1024))}MB"


@contextlib.contextmanager
def project_lock(folder: Path, name: str = LOCK_FILE) -> Iterator[None]:
    """An exclusive OS lock on `<folder>/<name>` (`lake.lock` by default), released on exit and
    when the process dies."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / name).open("a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProjectLocked from None
        yield
    finally:
        handle.close()  # closing releases the lock


def _check_semantic(checked: Checked, out: IO[str]) -> bool:
    """The project's semantic model compiled against its lake's current snapshot (#358),
    reported after every transform problem; True when it has problems. Without a lake or a
    snapshot yet, or while a run holds the project, it is not checked, and says so."""
    project = checked.project
    if project is None or checked.place is None or not semantic_model.model_files(project.folder):
        return False
    identity = load_identity()
    home = project.folder / f".{identity.slug}"
    if not lake_place.exists(home):
        print("semantic model not checked: the lake has no snapshot yet", file=out)
        return False
    try:
        with project_lock(home):
            lake = open_existing(home, checked.place)
            try:
                loaded = semantic_model.load_semantic(
                    project.folder,
                    lake,
                    lake.current_snapshot().id,
                    (identity.ossie_vendor_name, *identity.previous_vendor_names),
                    connections=project.connections,
                    audience="builder",  # local mode: whoever opens the project builds it
                )
            finally:
                lake.close()
    except ProjectLocked:
        print("semantic model not checked: a run is in progress", file=out)
        return False
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return True
    except StoreError as error:
        print(_store_problem(error, ".", "semantic model"), file=out)
        return True
    for problem in loaded.problems:
        print(problem, file=out)
    return bool(loaded.problems)


def validate(folder: Path, out: IO[str], show_sql: str | None = None) -> int:
    """Check a project; `show_sql`: also print the SQL that runs for that model (views inlined,
    the automatic incremental filter placed)."""
    checked = check_project(folder)
    for problem in checked.problems:
        print(problem, file=out)
    semantic = _check_semantic(checked, out)
    if checked.problems or semantic:
        return 1
    if show_sql is not None:
        by_name = {model.name: model for model in checked.discovery.models}
        model = by_name.get(show_sql)
        if model is None:
            print(
                Problem("MODEL_REFERENCE_UNKNOWN", ".", None, f"{show_sql} is not a model."),
                file=out,
            )
            return 1
        print(compiled_sql(model, by_name, checked.filtered.get(show_sql)), file=out)
        return 0
    print(f"{len(checked.discovery.models)} models, no problems.", file=out)
    return 0


class RunControl:
    """Cancels a run from another thread (the scheduler's stop, #69): the run's transaction is
    cancelled, its running statement interrupted, and nothing of it is committed. A cancel that
    comes before the transaction opens cancels it as soon as it does."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transaction: Transaction | None = None
        self._cancelled = False

    def attach(self, transaction: Transaction) -> None:
        with self._lock:
            self._transaction = transaction
            cancelled = self._cancelled
        if cancelled:
            transaction.cancel()

    def detach(self) -> None:
        with self._lock:
            self._transaction = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            transaction = self._transaction
        if transaction is not None:
            transaction.cancel()


@dataclass(frozen=True, slots=True)
class Selection:
    """A scheduled run: the models to build (dependencies first) and how to record it."""

    models: tuple[str, ...]
    trigger: Literal["freshness", "schedule"]
    full_refresh: frozenset[str] = frozenset()  # recorded as full_refresh (incremental: rebuilt)
    control: RunControl | None = None  # lets the scheduler cancel the run when it stops


def run(
    folder: Path,
    identity: Identity,
    out: IO[str],
    full_refresh: Sequence[str] | None = None,
    selection: Selection | None = None,
    drop_leftovers: bool = False,
) -> int:
    """Build every model, or only the `selection` of a scheduled run. `full_refresh`:
    incremental models to rebuild from every row (all of them when empty; None: none).
    `drop_leftovers`: also drop, in the run's transaction, the lake tables no model stores
    now (#88, decision #233); never in a scheduled run."""
    if drop_leftovers and selection is not None:
        print(
            Problem(
                "RUN_OPTION_INVALID",
                ".",
                None,
                "--drop-leftovers is a manual step; a scheduled run never drops tables.",
            ),
            file=out,
        )
        return 1
    checked = check_project(folder, identity)
    if checked.problems or checked.project is None or checked.graph is None:
        for problem in checked.problems:
            print(problem, file=out)
        return 1
    project, graph = checked.project, checked.graph
    incremental_names = {
        m.name for m in checked.discovery.models if m.config.materialized == "incremental"
    }
    refreshed: frozenset[str] = frozenset()
    if full_refresh is not None:
        unknown = sorted(set(full_refresh) - incremental_names)
        if unknown:
            for name in unknown:
                print(
                    Problem(
                        "MODEL_REFERENCE_UNKNOWN",
                        ".",
                        None,
                        f"--full-refresh {name}: not an incremental model of the project.",
                    ),
                    file=out,
                )
            return 1
        refreshed = frozenset(full_refresh or incremental_names)
    if selection is not None:
        refreshed = selection.full_refresh & incremental_names
    home = project.folder / f".{identity.slug}"
    try:
        with project_lock(home):
            return _run_locked(
                project, graph, checked, home, identity, out, refreshed, selection, drop_leftovers
            )
    except ProjectLocked:
        print(
            Problem(
                "PROJECT_LOCKED",
                f".{identity.slug}/{LOCK_FILE}",
                None,
                "another process is running this project; try again when it ends.",
            ),
            file=out,
        )
        return 1


@contextlib.contextmanager
def _controlled(control: RunControl | None, transaction: Transaction) -> Iterator[None]:
    """While the block runs, `control` can cancel `transaction`."""
    if control is None:
        yield
        return
    control.attach(transaction)
    try:
        yield
    finally:
        control.detach()


def _trigger(
    name: str, refreshed: frozenset[str], selection: Selection | None
) -> Literal["manual", "schedule", "freshness", "full_refresh"]:
    if name in refreshed or (selection is not None and name in selection.full_refresh):
        return "full_refresh"
    return "manual" if selection is None else selection.trigger


def _open_recorded(home: Path, place: Place, attachments: Sequence[Attachment] = ()) -> Lake:
    """Open the project's lake for a run, its record written first (`creating`) and confirmed
    once it opened, so no lake is ever left without a record (#146). A failed first open drops
    its record when nothing was created."""
    _refuse_missing_catalog(home)
    lake_place.begin(home, place)
    try:
        lake = open_lake(place.location, _limits(), attachments)
    except BaseException:
        # Ctrl-C or any error: the record goes only when nothing was created (#181). A record
        # that cannot be removed stays `creating`, which binds nothing that was not created; the
        # open's own error is the one reported (#192).
        with contextlib.suppress(ProjectLoadError):
            lake_place.abandon(home, place)
        raise
    try:
        lake_place.confirm(home, place)
        lake_place.complete(home, place)  # a record written before #147 gets its endpoint
    except BaseException:
        lake.close()
        raise
    return lake


def open_existing(home: Path, place: Place, memory: str | None = None) -> Lake:
    """Open the project's existing lake (with `memory`, its share), under the project lock, after
    checking again that its settings are the lake's own: another process may have created the
    lake since the project was checked (#181, Reviewer pass 2). `ProjectLoadError` otherwise."""
    lake_place.check_unchanged(home, place)
    _refuse_missing_catalog(home)
    lake_place.adopt(home, place)  # a default lake made before #204 gets its record
    lake = open_lake(place.location, _limits(memory))
    try:
        lake_place.complete(home, place)  # a record written before #147 gets its endpoint
    except BaseException:
        lake.close()
        raise
    return lake


def _refuse_missing_catalog(home: Path) -> None:
    """`STORE_CATALOG_UNAVAILABLE` when the project's confirmed lake has lost its DuckDB or
    SQLite catalog file: opening the place again would create a new, empty lake there and hide
    its history (#192). Nothing is opened or created."""
    missing = lake_place.missing_catalog(home)
    data = lake_place.orphan_data(home)
    if missing is not None and data is not None:
        raise StoreError(
            "STORE_CATALOG_UNAVAILABLE",
            f"The lake's catalog file {missing} is missing, but the data folder {data} holds "
            "data files of an earlier lake. The catalog holds the lake's history (runs, "
            "watermarks, snapshots), so a new one is never created next to them: restore the "
            f"file, or move {home.name}/{lake_place.LAKE_FOLDER} away (to keep the data files) "
            "to start a new, empty lake.",
            {"path": missing, "data": data},
        )
    if missing is not None:
        raise StoreError(
            "STORE_CATALOG_UNAVAILABLE",
            f"The lake's catalog file {missing} is missing. It holds the lake's history (runs, "
            "watermarks, snapshots), so a new one is never created in its place: restore the "
            f"file (or connect the disk it is on), or remove {home.name}/{lake_place.RECORD_FILE} "
            f"and {home.name}/{lake_place.LAKE_FOLDER} (move them away to keep the data files) "
            "to start a new, empty lake.",
            {"path": missing},
        )


def _limits(memory: str | None = None) -> Limits:
    return Limits(
        memory_limit=memory or memory_limit(),
        threads=os.cpu_count() or 2,
        max_concurrent=4,
        queue_size=16,
        default_timeout_ms=DEFAULT_TIMEOUT_MS,
    )


def _git(folder: Path, *arguments: str) -> str | None:
    """Output of a git command in `folder`, or None when git or the repository is missing."""
    try:
        result = subprocess.run(
            # No fsmonitor hook and no index rewrite: reading the commit must not run the
            # project's configured programs or change its files.
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "--no-optional-locks",
                "-C",
                str(folder),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 else None


def _project_commit(folder: Path) -> tuple[str | None, bool | None]:
    commit = _git(folder, "rev-parse", "HEAD")
    if commit is None or not commit.strip():
        return None, None
    status = _git(
        folder, "status", "--porcelain", "--", "models", "project.yaml", "connections.yaml"
    )
    return commit.strip(), None if status is None else bool(status.strip())


def _versions(identity: Identity) -> Versions:
    return Versions(
        duckdb=importlib.metadata.version("duckdb"),
        modules=[
            ModuleVersion(name=name, version=number) for name, number in module_versions(identity)
        ],
    )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")


@dataclass(slots=True)
class _Outcome:
    model: Model
    started_at: str
    finished_at: str
    outcome: str = "not_run"
    rows_read: str | None = None
    rows_written: str | None = None
    total: str | None = None  # an incremental table's rows after the run (#257)
    error: RunError | None = None
    since: str | None = None
    watermark: str | None = None
    tests: list[CheckResult] = field(default_factory=list)
    reconciliation: list[CheckResult] = field(default_factory=list)
    started: bool = False
    lineage_run_id: str = field(default_factory=lambda: str(uuid.uuid7()))


def _run_locked(
    project: Project,
    graph: Graph,
    checked: Checked,
    home: Path,
    identity: Identity,
    out: IO[str],
    refreshed: frozenset[str] = frozenset(),
    selection: Selection | None = None,
    drop_leftovers: bool = False,
) -> int:
    discovery = checked.discovery
    run_id = str(uuid.uuid4())
    print(f"run {run_id}", file=out)
    commit, dirty = _project_commit(project.folder)
    started = _now()
    snapshot: int | None = None
    failures: list[Problem] = []
    lake = None
    open_error: StoreError | None = None
    assert checked.place is not None  # a refused place is a problem: no run
    place = checked.place
    try:
        # Checked again under the project lock: another process may have started this lake's
        # first open since the project was checked (#181, Reviewer pass 1).
        lake_place.check_unchanged(home, place)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    try:
        lake = _open_recorded(home, place, source_attachments(project.sources))
    except StoreError as error:
        open_error = error
    except ProjectLoadError as error:  # the lake record cannot be written (#192)
        print(_load_problem(error), file=out)
        return 1
    existing: dict[str, Columns | None] = {}
    previous: dict[str, str | None] = {}
    since: dict[str, str] = {}
    leftovers: list[tuple[str, str]] = []
    if lake is not None:
        try:
            leftovers = _leftovers(lake, discovery.models)
            for model in discovery.models:
                if model.config.materialized == "incremental":
                    existing[model.name] = _table_columns(lake, model)
                    previous[model.name], since[model.name] = _since(
                        lake,
                        model,
                        checked.columns.get(model.name, []),
                        existing[model.name],
                        model.name in refreshed,
                    )
        except StoreError as error:
            lake.close()
            lake, open_error = None, error
    plan = plan_run(
        discovery.models,
        graph,
        checked.columns,
        existing,
        since,
        refreshed,
        previous,
        checked.differences,
        checked.filtered,
        databases(project),
    )
    if selection is not None:
        plan = [(model, steps) for model, steps in plan if model.name in selection.models]
    outcomes = [
        _Outcome(
            model,
            started,
            started,
            since=since.get(model.name),
            watermark=previous.get(model.name),
        )
        for model, _ in plan
    ]
    if open_error is not None:
        failures = [_store_problem(open_error, ".", "")]
        for item in outcomes:
            item.outcome = "failed"
            item.error = RunError(code=open_error.code, message=open_error.message)
    if lake is None:
        # The run could not start (a source is unreachable, say): it is still recorded, in a
        # lake opened without sources.
        try:
            lake = _open_recorded(home, place)
        except StoreError as error:
            for problem in failures or [_store_problem(error, ".", "")]:
                print(problem, file=out)
            return 1
        except ProjectLoadError as error:  # the lake record cannot be written (#192)
            print(_load_problem(error), file=out)
            return 1
    interrupted = False
    recorded = False  # the success records committed with the run's data (#394)
    warning: Problem | None = None

    def records() -> list[RunRecord]:
        return _records(outcomes, run_id, commit, dirty, identity, refreshed, selection)

    try:
        current: _Outcome | None = None
        finished = False  # the block ran to its end: what fails after it is the commit
        try:
            control = selection.control if selection is not None else None
            with lake.transaction() as transaction, _controlled(control, transaction):
                for item, (_, steps) in zip(outcomes, plan, strict=True):
                    if failures:
                        break
                    current = item
                    item.started_at = _now()
                    item.started = True
                    for step in steps:
                        _run_step(transaction, step, item)
                    _check_tests(item)
                    item.finished_at = _now()
                    item.outcome = "ran"
                current = None
                if failures:
                    raise _NothingToCommit
                if drop_leftovers:
                    for statement in _drop_statements(leftovers):
                        transaction.execute(statement)
                # The records commit with the data, so a stop at any point leaves both or
                # neither (#394). Rows are counted here, in the transaction that wrote them.
                for item in outcomes:
                    if item.rows_written is None and item.model.config.materialized != "view":
                        item.rows_written = _count(transaction, item.model)
                    if item.model.config.materialized == "incremental":
                        item.total = _count(transaction, item.model)
                    item.outcome = "success"
                for statement in record_statements(records()):
                    transaction.execute(statement)
                finished = True
            recorded = True
        except _NothingToCommit:
            pass
        except (StoreError, _StepFailed, KeyboardInterrupt) as error:
            # Ctrl-C: the run is recorded, then the interruption goes on.
            interrupted = isinstance(error, KeyboardInterrupt)
            committed = _committed_snapshot(lake, run_id) if finished else None
            if committed is not None:
                # Stopped after the commit (the token lookup, say): the run and its records
                # are committed, so no failed record is added.
                recorded, snapshot = True, committed
                if isinstance(error, StoreError):
                    warning = _store_problem(error, ".", "after the commit")
            else:
                failures = _rolled_back(error, outcomes, current)
        if recorded and snapshot is None:
            # The snapshot this run committed, found by its token, never the latest one:
            # another clone sharing the lake may have committed since (#255).
            result = transaction.result
            if result is not None and result.snapshot is not None:
                snapshot = result.snapshot.id
            else:
                warning = _store_problem(
                    StoreError(
                        "STORE_SNAPSHOT_NOT_FOUND", "The run's snapshot could not be read.", {}
                    ),
                    ".",
                    "after the commit",
                )
        events: list[lineage.Event] = []
        lineage_failure: Problem | None = None
        try:
            events = _lineage_events(project, identity, run_id, outcomes, checked, lake)
        except Exception as error:  # noqa: BLE001 (lineage must never change the run's outcome)
            lineage_failure = Problem(
                "LINEAGE_NOT_SENT",
                ".",
                None,
                f"lineage events could not be built ({type(error).__name__}).",
            )
        records_error: StoreError | None = None
        if not recorded:
            # Nothing of the run committed: it is recorded in a transaction of its own.
            try:
                with lake.transaction() as transaction:
                    for statement in record_statements(records()):
                        transaction.execute(statement)
            except StoreError as error:
                records_error = error
        evidence: list[str] = []
        if not failures and not interrupted and records_error is None and snapshot is not None:
            written = [
                item.model.name
                for item in outcomes
                if item.outcome == "success" and item.model.config.materialized != "view"
            ]
            evidence = _evidence_after_run(project, identity, lake, snapshot, written, run_id)
    finally:
        lake.close()
    if interrupted:
        for problem in failures:
            print(problem, file=out)
        if records_error is not None:
            print(_store_problem(records_error, ".", "run records were not written"), file=out)
        raise KeyboardInterrupt
    lineage_warning = lineage_failure or _publish_lineage(project, home, run_id, events)
    if records_error is not None:
        for problem in failures:
            print(problem, file=out)
        print(_store_problem(records_error, ".", "run records were not written"), file=out)
        if lineage_warning is not None:
            print(lineage_warning, file=out)
        return 1
    if failures:
        for problem in failures:
            print(problem, file=out)
        if lineage_warning is not None:
            print(lineage_warning, file=out)
        return 1
    for notice in (warning, lineage_warning):
        if notice is not None:
            print(notice, file=out)
    print(f"snapshot {snapshot if snapshot is not None else '-'}", file=out)
    for line in evidence:
        print(line, file=out)
    for item in outcomes:
        if item.model.config.materialized == "view":
            print(f"tested {item.model.name}", file=out)  # a view is not stored
        else:
            rows = "unknown" if item.rows_written is None else item.rows_written
            if item.model.config.materialized == "incremental":
                # New rows, and the table they joined (#257): 0 new rows is not an empty table.
                total = "table total unknown" if item.total is None else f"{item.total} in table"
                print(f"built {item.model.name}: {rows} new rows ({total})", file=out)
            else:
                print(f"built {item.model.name}: {rows} rows", file=out)
    for schema, table in leftovers:
        if drop_leftovers:
            print(f"dropped {schema}.{table}", file=out)
        else:
            print(
                f"leftover {schema}.{table}: no model stores it; kept as it was "
                "(remove it with --drop-leftovers)",
                file=out,
            )
    return 0


def _evidence_after_run(
    project: Project,
    identity: Identity,
    lake: Lake,
    snapshot: int,
    written: Sequence[str],
    run_id: str,
) -> list[str]:
    """The evidence of every dataset a successful run wrote, and of the relationships touching
    one, checked on the snapshot the run committed and recorded (#355). The run stays as it
    went: a semantic model with problems, or a check that cannot run, is reported only."""
    if not written or not semantic_model.model_files(project.folder):
        return []
    loaded = semantic_model.load_semantic(
        project.folder,
        lake,
        snapshot,
        (identity.ossie_vendor_name, *identity.previous_vendor_names),
        connections=project.connections,
        audience="builder",  # local mode: whoever opens the project builds it
    )
    if loaded.model is None:
        return ["evidence not checked: the semantic model has problems", *map(str, loaded.problems)]
    datasets, _ = evidence_runs.touching(loaded.model, written)
    if not datasets:
        return []
    return evidence_runs.verify_and_store(loaded.model, lake, datasets=datasets, run=run_id).lines()


def evidence(folder: Path, identity: Identity, out: IO[str], dataset: str | None = None) -> int:
    """`<slug> evidence`: the project's builder checks the evidence of one dataset, or of every
    one, on the lake's current snapshot (#355). In local mode, whoever opens the project is its
    builder (Owner decision on #358); this grant is the CLI's, never the HTTP API's."""
    try:
        project = load_project(folder)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    home = project.folder / f".{identity.slug}"
    if not lake_place.exists(home):
        print("evidence not checked: the lake has no snapshot yet", file=out)
        return 1
    try:
        with project_lock(home):
            lake = open_existing(home, lake_location(project, home))
            try:
                loaded = semantic_model.load_semantic(
                    project.folder,
                    lake,
                    lake.current_snapshot().id,
                    (identity.ossie_vendor_name, *identity.previous_vendor_names),
                    connections=project.connections,
                    audience="builder",
                )
                if loaded.model is None:
                    for problem in loaded.problems or (
                        Problem("OSSIE_INVALID", f"{semantic_model.FOLDER}/", None, "no model"),
                    ):
                        print(problem, file=out)
                    return 1
                chosen = None
                if dataset is not None:
                    document = loaded.model.compiled.ossie.document
                    names = {
                        sql.normalize(str(d["name"]))
                        for d in evidence_runs.items(document, "datasets")
                    }
                    if sql.normalize(dataset) not in names:
                        print(f"OSSIE_INVALID {loaded.model.file} no dataset {dataset!r}", file=out)
                        return 1
                    chosen = {sql.normalize(dataset)}
                outcome = evidence_runs.verify_and_store(loaded.model, lake, datasets=chosen)
            finally:
                lake.close()
    except ProjectLocked:
        print("evidence not checked: a run is in progress", file=out)
        return 1
    except StoreError as error:
        print(_store_problem(error, ".", "evidence"), file=out)
        return 1
    for line in outcome.lines():
        print(line, file=out)
    return 1 if outcome.problems else 0


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


LINEAGE_FOLDER = "lineage"
LINEAGE_TIMEOUT_S = 10  # each request
LINEAGE_DEADLINE_S = 30  # all of a run's events together


def _lineage_events(
    project: Project,
    identity: Identity,
    run_id: str,
    outcomes: list[_Outcome],
    checked: Checked,
    lake: Lake,
) -> list[lineage.Event]:
    """OpenLineage events of the models that started (none for those that never did)."""
    key = lineage.project_key(project.settings.name)
    versions = dict(module_versions(identity))
    scope = lineage.Scope(
        producer=f"urn:{identity.slug}:transform:{versions.get('transform', 'unknown')}",
        job_namespace=f"{identity.slug}://{key}",
        lake_namespace=f"ducklake://{key}",
    )
    by_name = {model.name: model for model in checked.discovery.models}
    described: dict[tuple[str, ...], list[str] | None] = {}

    def columns_of(path: list[str]) -> list[str] | None:
        """The columns of a relation the model reads, from the binder (None when unknown)."""
        parts = tuple(path[1:] if len(path) == 3 and path[0] == "lake" else path)
        if parts not in described:
            relation = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
            try:
                described[parts] = [name for name, _ in lake.describe(f"SELECT * FROM {relation}")]
            except StoreError:
                described[parts] = None
        return described[parts]

    # Models rolled back by a later failure end when the run did, not when they finished.
    ended = max((item.finished_at for item in outcomes if item.started), default=_now())
    runs: list[lineage.ModelRun] = []
    for item in outcomes:
        if not item.started:
            continue
        # Column lineage pairs the SELECT's items with its own columns, in its order; the
        # schema facet shows the table as stored (columns added later go to its end).
        selected: Columns = checked.columns.get(item.model.name, [])
        output: Columns = selected
        if item.outcome == "success" and item.model.config.materialized != "view":
            # When the table cannot be read, the schema facet keeps the bound columns, if any.
            with contextlib.suppress(StoreError):
                output = _table_columns(lake, item.model) or output
        runs.append(
            lineage.ModelRun(
                model=item.model,
                run_id=item.lineage_run_id,
                started_at=item.started_at,
                finished_at=item.finished_at if item.outcome == "success" else ended,
                outcome="success" if item.outcome == "success" else "failed",
                output=output,
                inputs=lineage.inputs(item.model, by_name, project.sources, scope.lake_namespace),
                column_lineage=lineage.column_lineage(
                    item.model,
                    selected or output,
                    by_name,
                    project.sources,
                    scope.lake_namespace,
                    columns_of,
                ),
                rows_written=item.rows_written if item.outcome == "success" else None,
                error=None if item.error is None else f"{item.error.code}: {item.error.message}",
            )
        )
    return lineage.events(scope, run_id, runs)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None  # the 3xx response is then raised as an HTTPError


def _publish_lineage(
    project: Project, home: Path, run_id: str, events: list[lineage.Event]
) -> Problem | None:
    """Write the run's events to `<home>/lineage/<run_id>.ndjson`, then POST them to the
    project's collector when one is declared; a failure is a warning, never the run's outcome."""
    if not events:
        return None
    folder = home / LINEAGE_FOLDER
    path = folder / f"{run_id}.ndjson"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
    except OSError as error:
        return Problem(
            "LINEAGE_NOT_SENT",
            f"{home.name}/{LINEAGE_FOLDER}",
            None,
            f"the lineage events could not be written ({error.strerror or type(error).__name__}).",
        )
    settings = project.settings.lineage
    if settings is None:
        return None
    headers = {"Content-Type": "application/json"}
    if settings.api_key is not None:
        try:
            headers["Authorization"] = f"Bearer {resolve_secret(settings.api_key)}"
        except ProjectLoadError as error:
            return Problem("LINEAGE_NOT_SENT", "project.yaml", None, error.error.message)
    # Redirects are refused: following one would turn the POST into a GET without the event,
    # and would carry the key to another host.
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if urllib.parse.urlsplit(settings.url).scheme == "http":
        # A collector on this machine is reached directly: a proxy would see the key in clear.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + LINEAGE_DEADLINE_S
    for event in events:
        remaining = deadline - time.monotonic()
        request = urllib.request.Request(
            settings.url,
            data=json.dumps(event).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            if remaining <= 0:
                raise TimeoutError
            with opener.open(request, timeout=min(LINEAGE_TIMEOUT_S, remaining)) as response:
                response.read()
        except Exception as error:  # noqa: BLE001 (any failure is a warning, never the run's)
            if isinstance(error, urllib.error.HTTPError):
                error.close()  # an HTTP error keeps the response open until closed
            # Never the key or the URL: only the kind of failure and the file with the events.
            reason = getattr(error, "code", None) or type(error).__name__
            return Problem(
                "LINEAGE_NOT_SENT",
                f"{home.name}/{LINEAGE_FOLDER}/{path.name}",
                None,
                f"the lineage collector did not accept the events ({reason}); they are kept "
                "in this file.",
            )
    return None


class _StepFailed(Exception):
    """A model's run step refused to go on (a check failed, or the model cannot run)."""

    def __init__(self, code: str, message: str, problems: Sequence[Problem] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = tuple(problems)


def _check_tests(item: _Outcome) -> None:
    """After every test and reconciliation check of a model ran: the run stops if any failed
    (MODEL_TEST_FAILED first, then RECONCILIATION_FAILED); every result is recorded."""
    file = item.model.config_file or item.model.sql_file
    for code, results, unit in (
        ("MODEL_TEST_FAILED", item.tests, "rows"),
        ("RECONCILIATION_FAILED", item.reconciliation, "groups"),
    ):
        failed = [result for result in results if result.outcome == "failed"]
        if not failed:
            continue
        lines = [f"{result.name} failed on {result.failing_rows} {unit}" for result in failed]
        raise _StepFailed(
            code,
            f"{item.model.name}: " + "; ".join(lines) + ".",
            [Problem(code, file, None, line) for line in lines],
        )


def _run_step(transaction: Transaction, step: Step | ModelTest | Reconcile, item: _Outcome) -> None:
    if isinstance(step, Reconcile) and step.land is not None:
        # The database source's groups, landed from its worker, compared, then dropped (#372).
        landing = step.land
        transaction.land(landing.source, landing.sql, [], landing.schema, landing.table)
        failing = transaction.value(step.request) or "0"
        staged = f'"{landing.schema}"."{landing.table}"'
        for sql in (f"DROP TABLE {staged}", f'DROP SCHEMA IF EXISTS "{landing.schema}"'):
            transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
        item.reconciliation.append(
            CheckResult(
                name=step.name,
                outcome="passed" if failing == "0" else "failed",
                failing_rows=failing,
            )
        )
    elif isinstance(step, ModelTest | Reconcile):
        failing = transaction.value(step.request) or "0"
        results = item.tests if isinstance(step, ModelTest) else item.reconciliation
        results.append(
            CheckResult(
                name=step.name,
                outcome="passed" if failing == "0" else "failed",
                failing_rows=failing,
            )
        )
    elif isinstance(step, StatementRequest):
        transaction.execute(step)
    elif isinstance(step, Land):
        # A database source, read in its own worker and landed as one Arrow stream (#372).
        transaction.land(step.source, step.sql, list(step.parameters), step.schema, step.table)
    elif isinstance(step, Refuse):
        raise _StepFailed(step.code, step.message)
    elif isinstance(step, Watermark):
        # Stored with the rows, in this transaction: it moves only if they commit.
        value = transaction.value(step.request)
        for statement in incremental.watermark_statements(step, value):
            transaction.execute(statement)
        item.watermark = value
    elif isinstance(step, Count):
        value = transaction.value(StatementRequest(kind="statement", sql=step.sql, parameters=[]))
        item.rows_read = value or "0"
        item.rows_written = item.rows_read
    else:
        value = transaction.value(
            StatementRequest(kind="statement", sql=step.sql, parameters=list(step.parameters))
        )
        if value not in {None, "0"}:
            raise _StepFailed(step.code, step.failure(value))


def _since(
    lake: Lake, model: Model, output: Columns, table: Columns | None, full_refresh: bool
) -> tuple[str | None, str]:
    """The model's previous watermark and its `$since`.

    No table, or a full refresh: no watermark, `$since` is `-infinity` (every row loads). Else
    the watermark stored with its table; for a table written before watermarks were stored, the
    largest `time_column` in it.
    """
    incremental_config = model.config.incremental
    assert incremental_config is not None
    time_column = incremental_config.time_column.root.lower()
    kind = {name.lower(): typ for name, typ in output}.get(time_column, "")
    watermark: str | None = None
    if table is not None and not full_refresh:
        snapshot = lake.current_snapshot().id
        try:
            rows = _column(lake, incremental.watermark_request(snapshot, model))
        except StoreError as error:
            if error.code != "STORE_STATEMENT_INVALID":
                raise
            rows = []  # no watermarks table yet
        if not rows and kind in incremental.PARAMETER_TYPES:
            rows = _column(lake, incremental.table_watermark_request(snapshot, model, kind))
        watermark = rows[0] if rows else None
    return watermark, incremental.since_value(kind, watermark, incremental_config.lookback)


def _column(lake: Lake, request: ReadRequest) -> list[str | None]:
    values: list[str | None] = lake.read(request).read_all().column("watermark").to_pylist()
    return values


def _table_columns(lake: Lake, model: Model) -> Columns | None:
    """The columns of a model's table in the lake now, or None when it does not exist."""
    request = ReadRequest(
        kind="read",
        sql=f'SELECT * FROM "{model.schema}"."{model.table}" LIMIT 0',
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=1,
    )
    try:
        schema = lake.read(request).schema
    except StoreError as error:
        if error.code == "STORE_STATEMENT_INVALID":
            return None  # not a table of the lake (yet)
        raise
    return [
        (field.name, (field.metadata or {}).get(b"duckdb_type", b"").decode()) for field in schema
    ]


class _NothingToCommit(Exception):
    """Leaves the run transaction without changing anything (the run could not start)."""


def _store_message(error: StoreError) -> str:
    detail = error.details.get("duckdb", "")
    return error.message + (f" {detail}" if detail else "")


def _store_problem(error: StoreError, file: str, model: str) -> Problem:
    prefix = f"{model}: " if model else ""
    return Problem(error.code, file, None, prefix + _store_message(error))


def _count(transaction: Transaction, model: Model) -> str | None:
    """The rows of `model`'s table as this transaction leaves them, exact digits (#394)."""
    return transaction.value(
        StatementRequest(
            kind="statement",
            sql=f'SELECT count(*) FROM "{model.schema}"."{model.table}"',
            parameters=[],
        )
    )


def _committed_snapshot(lake: Lake, run_id: str) -> int | None:
    """The snapshot holding run `run_id`'s success records, or None when it committed none: a
    run stopped after its block ran learns from its own records whether the commit happened
    (#394). None too when that cannot be read: the run is then recorded as rolled back."""
    try:
        tables = lake.tables()  # before the snapshot: record tables are never dropped
        if ("_meta", "run_records_v2") not in tables:
            return None
        request = committed_request(lake.current_snapshot().id, run_id)
        found: int | None = lake.read(request).read_all().column("snapshot")[0].as_py()
    except StoreError:
        return None
    return found


def _rolled_back(
    error: StoreError | _StepFailed | KeyboardInterrupt,
    outcomes: Sequence[_Outcome],
    current: _Outcome | None,
) -> list[Problem]:
    """Mark the models of a run that rolled back, with the reason; the run's problems."""
    for item in outcomes:
        if item.outcome in {"ran", "success"}:
            item.outcome = "failed"  # rolled back with the run
    if isinstance(error, KeyboardInterrupt):
        reason = RunError(
            code="STORE_CANCELLED",
            message="The run was interrupted; it rolled back and nothing was written.",
        )
    elif isinstance(error, _StepFailed):
        reason = RunError(code=error.code, message=error.message)
    else:
        reason = RunError(code=error.code, message=_store_message(error))
    if current is not None:
        current.finished_at = _now()
        current.outcome = "failed"
        current.error = reason
        if isinstance(error, _StepFailed) and error.problems:
            return list(error.problems)
        return [
            Problem(
                reason.code,
                current.model.sql_file,
                None,
                f"{current.model.name}: {reason.message}"
                if isinstance(error, StoreError)
                else reason.message,
            )
        ]
    # Not a model's fault (the transaction could not start or commit, or the records could not
    # be written): every model failed for the same reason.
    for item in outcomes:
        item.outcome = "failed"
        item.error = reason
    return [Problem(reason.code, ".", None, reason.message)]


def _records(
    outcomes: Sequence[_Outcome],
    run_id: str,
    commit: str | None,
    dirty: bool | None,
    identity: Identity,
    refreshed: frozenset[str],
    selection: Selection | None,
) -> list[RunRecord]:
    """One record per model. `snapshot_id` is not stored: it is the snapshot the records
    commit in, resolved when they are read (#394)."""
    return [
        RunRecord(
            run_id=run_id,
            model=item.model.name,
            trigger=_trigger(item.model.name, refreshed, selection),
            materialized=item.model.config.materialized or "table",
            project_commit=commit,
            project_dirty=dirty,
            versions=_versions(identity),
            since=item.since,
            rows_read=None if item.rows_read is None else RowsRead(item.rows_read),
            rows_written=None
            if item.rows_written is None or item.outcome != "success"
            else RowsWritten(item.rows_written),
            watermark=item.watermark if item.outcome == "success" else None,
            tests=item.tests,
            reconciliation=item.reconciliation,
            snapshot_id=None,
            started_at=item.started_at,
            finished_at=item.finished_at,
            outcome=item.outcome,  # type: ignore[arg-type]  # success, failed or not_run
            error=item.error,
        )
        for item in outcomes
    ]


def runs(folder: Path, identity: Identity, limit: int, out: IO[str]) -> int:
    """Recent runs of a project, newest first, with times in the project's time zone."""
    try:
        project = load_project(folder)
        home = project.folder / f".{identity.slug}"
        place = lake_location(project, home)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    zone = project.settings.time_zone
    print(heartbeat.describe(home, zone, dt.datetime.fromisoformat(_now())), file=out)
    if not lake_place.exists(home):
        print("no runs yet", file=out)
        return 0
    try:
        with project_lock(home):
            lake = open_existing(home, place)
            try:
                # The tables before the snapshot: record tables are never dropped, so each one
                # listed exists at the snapshot read after them (#394).
                tables = lake.tables()
                snapshot = lake.current_snapshot().id
                request = history_request(snapshot, zone, limit, tables)
                # No request: the lake has no record table yet.
                rows = [] if request is None else lake.read(request).read_all().to_pylist()
            finally:
                lake.close()
    except ProjectLocked:
        print(
            Problem(
                "PROJECT_LOCKED", f".{identity.slug}/{LOCK_FILE}", None, "a run is in progress."
            ),
            file=out,
        )
        return 1
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    except StoreError as error:
        print(_store_problem(error, ".", ""), file=out)
        return 1
    if not rows:
        print("no runs yet", file=out)
    for row in rows:
        print(
            f"{row['run_id']}  {row['started']}  {row['outcome']:<7}  "
            f"snapshot {row['snapshot'] if row['snapshot'] is not None else '-'}  {row['models']}",
            file=out,
        )
    return 0
