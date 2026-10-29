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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.project.v1 import ClickhouseConnection
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
from orvex.server.identity import Identity
from orvex.server.project import (
    Connection,
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
    clickhouse,
    incremental,
    lineage,
    model_tests,
    reconciliation,
    schedule,
)
from orvex.transform.extraction import Extract
from orvex.transform.graph import Graph, build_graph, compiled_sql, plan_run, test_relations
from orvex.transform.incremental import Columns, Count, Refuse, Step, Watermark
from orvex.transform.model_tests import ModelTest
from orvex.transform.models import Discovery, Model, discover_models
from orvex.transform.reconciliation import Comparison, Reconcile
from orvex.transform.run_records import history_request, record_statements
from orvex.transform.sources import source_attachments

LOCK_FILE = "lake.lock"
LAKE_FOLDER = "lake"
DEFAULT_MEMORY = "4GB"
DEFAULT_TIMEOUT_MS = 3_600_000
GIT_TIMEOUT_S = 10


@dataclass(frozen=True, slots=True)
class Problem:
    code: str
    file: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        return f"{self.code} {where} {self.message}"


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
    # Extraction models: their validated ClickHouse output columns (P1-17).
    extracted: Mapping[str, tuple[clickhouse.Column, ...]] = field(default_factory=dict)


class ProjectLocked(Exception):
    """Another process holds the project's local lake."""


def _from_model_error(error: ModelError) -> Problem:
    details = error.details
    return Problem(error.code.root, details.file, details.line, error.message)


def check_project(folder: Path) -> Checked:
    """Load the project, discover its models and order them; every problem, nothing written."""
    try:
        project = load_project(folder)
    except ProjectLoadError as error:
        details = error.error.details
        problem = Problem(error.code, details.file or ".", details.line, error.error.message)
        return Checked(None, Discovery((), ()), None, (problem,))
    discovery = discover_models(project.folder, project.connections)
    sources = set(project.connections)
    graph = build_graph(discovery.models, sources)
    problems = [_from_model_error(item) for item in (*discovery.problems, *graph.problems)]
    kinds = {name: connection.type for name, connection in project.connections.items()}
    zone = project.settings.time_zone
    for model in discovery.models:
        problems += [_from_model_error(item) for item in schedule.config_problems(model, zone)]
        problems += [_from_model_error(item) for item in incremental.config_problems(model)]
        problems += [_from_model_error(item) for item in reconciliation.problems(model, kinds)]
        if model.extraction:
            problems += [_from_model_error(item) for item in clickhouse.since_problems(model)]
    columns: dict[str, Columns] = {}
    differences: dict[str, list[Comparison]] = {}
    filtered: dict[str, str] = {}
    extracted: dict[str, tuple[clickhouse.Column, ...]] = {}
    needs_binding = any(
        m.config.materialized == "incremental"
        or m.config.tests
        or m.config.reconciliation
        or m.extraction  # validated on its ClickHouse connection (P1-17)
        for m in discovery.models
    )
    if needs_binding and not problems:
        columns, differences, filtered, extracted, bind_problems = _bind(project, discovery, graph)
        problems += bind_problems
        problems += [
            _from_model_error(item) for item in incremental.problems_for(discovery.models, columns)
        ]
    return Checked(
        project, discovery, graph, tuple(problems), columns, differences, filtered, extracted
    )


def _bind(
    project: Project, discovery: Discovery, graph: Graph
) -> tuple[
    dict[str, Columns],
    dict[str, list[Comparison]],
    dict[str, str],
    dict[str, tuple[clickhouse.Column, ...]],
    list[Problem],
]:
    """Output columns of every model, from DuckDB's binder; nothing is executed.

    Binding happens in a scratch lake in a temporary folder (never the project's): upstream
    models are stand-in empty tables with the columns found for them, and sources are attached
    as for a run.
    """
    by_name = {model.name: model for model in discovery.models}
    relations = test_relations(discovery.models)
    columns: dict[str, Columns] = {}
    differences: dict[str, list[Comparison]] = {}
    filtered: dict[str, str] = {}
    extracted: dict[str, tuple[clickhouse.Column, ...]] = {}
    problems: list[Problem] = []
    failed: set[str] = set()
    scratch = Path(tempfile.mkdtemp(prefix="bind-"))
    try:
        location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(scratch))
        try:
            lake = open_lake(location, _limits(), source_attachments(project.connections))
        except StoreError as error:
            return {}, {}, {}, {}, [_store_problem(error, ".", "")]
        try:
            for name in graph.order:
                model = by_name[name]
                if failed & set(graph.dependencies.get(name, ())):
                    failed.add(name)
                    continue
                if model.extraction:
                    described = _describe_extraction(project, model)
                    if isinstance(described, Problem):
                        failed.add(name)
                        problems.append(described)
                        continue
                    extracted[name] = described
                    _stand_in(lake, model, [(c.name, c.mapped.duckdb) for c in described])
                    # DuckDB's own names for the mapped types, as for every other model.
                    columns[name] = lake.describe(_relation_sql(model))
                    if model.config.tests:
                        problems += _bind_tests(lake, model, columns, by_name, relations)
                    continue
                try:
                    output = _describe(lake, model, compiled_sql(model, by_name))
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
                columns[name] = output
                if _automatic(model):
                    proven = _prove_filter(lake, model, output, by_name)
                    if isinstance(proven, Problem):
                        problems.append(proven)
                    else:
                        filtered[name] = proven
                if model.config.materialized != "view":
                    _stand_in(lake, model, output)
                if model.config.tests:
                    problems += _bind_tests(lake, model, columns, by_name, relations)
                if model.config.reconciliation:
                    found, kinds = _bind_reconciliation(lake, model, relations[name])
                    problems += found
                    differences[name] = kinds
        finally:
            lake.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return columns, differences, filtered, extracted, problems


def _describe_extraction(project: Project, model: Model) -> tuple[clickhouse.Column, ...] | Problem:
    """An extraction model's output columns, mapped to DuckDB types: ClickHouse parses and
    describes its SQL on the model's connection, read-only; no row is read (P1-17)."""
    name = model.config.connection
    connection = project.connections.get(name or "")
    if not isinstance(connection, ClickhouseConnection):  # discovery already refused it
        return Problem("MODEL_CONFIG_INVALID", model.config_file or model.sql_file, None, "")
    password = clickhouse.read_password(connection)
    if password is None:
        variable = clickhouse.password_variable(connection)
        return Problem(
            "PROJECT_SECRET_MISSING",
            "connections.yaml",
            None,
            f"Environment variable {variable} is not set (the password of connection {name}).",
        )
    described = clickhouse.describe_model(model, connection, password)
    if isinstance(described, ModelError):
        return _from_model_error(described)
    return described.columns


def _relation_sql(model: Model) -> str:
    schema, table = (part.replace('"', '""') for part in (model.schema, model.table))
    return f'SELECT * FROM "{schema}"."{table}"'


def _automatic(model: Model) -> bool:
    incremental_config = model.config.incremental
    return (
        model.config.materialized == "incremental"
        and incremental_config is not None
        and (incremental_config.filter or "auto") == "auto"
        and not model.extraction
    )


def _prove_filter(
    lake: Lake, model: Model, output: Columns, by_name: Mapping[str, Model]
) -> str | Problem:
    """The model's SQL with the proven `> $since` filter, bound once to check it; or why the
    filter cannot be proven (`INCREMENTAL_FILTER_NOT_PROVABLE`, with the time column's line)."""
    incremental_config = model.config.incremental
    assert incremental_config is not None
    described: dict[tuple[str, ...], list[str] | None] = {}

    def columns_of(path: list[str]) -> list[str] | None:
        parts = tuple(path)
        if parts not in described:
            relation = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
            try:
                described[parts] = [name for name, _ in lake.describe(f"SELECT * FROM {relation}")]
            except StoreError:
                described[parts] = None
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
        rewritten = _describe(lake, model, compiled_sql(model, by_name, result))
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
    lake: Lake, model: Model, relation: str
) -> tuple[list[Problem], list[Comparison]]:
    """Each check bound on the model and its source: the type of its measure difference, which
    must be an exact number (integers or DECIMAL, law 11)."""
    file = model.config_file or model.sql_file
    problems: list[Problem] = []
    kinds: list[Comparison] = []
    for index, check in enumerate(model.config.reconciliation or []):
        pointer = f"/reconciliation/{index}"
        try:
            described = lake.describe(reconciliation.measures_sql(check, relation))
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


def _describe(lake: Lake, model: Model, sql: str) -> Columns:
    """Output columns of a model's SQL. An incremental model's `$since` is bound as a NULL of
    each temporal type in turn; the binding whose `time_column` has that type is the one a run
    will use (any other model has no parameter to bind)."""
    incremental_config = model.config.incremental
    if model.config.materialized != "incremental" or incremental_config is None:
        return lake.describe(sql)
    if not incremental.uses_since(sql):
        return lake.describe(sql)
    time_column = incremental_config.time_column.root.lower()
    first: Columns | None = None
    error: StoreError | None = None
    for output_type, parameter_type in incremental.PARAMETER_TYPES.items():
        since = Parameter(name=incremental.SINCE, type=ParameterType(parameter_type), value=None)
        try:
            output = lake.describe(sql, [since])
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


def _memory_limit() -> str:
    """Three quarters of the machine's memory, or 4GB when it cannot be read."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except AttributeError, ValueError, OSError:
        return DEFAULT_MEMORY
    return f"{max(1, total * 3 // 4 // (1024 * 1024))}MB"


@contextlib.contextmanager
def project_lock(folder: Path) -> Iterator[None]:
    """An exclusive OS lock on `<folder>/lake.lock`, released on exit and when the process dies."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / LOCK_FILE).open("a+b")
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


def validate(folder: Path, out: IO[str], show_sql: str | None = None) -> int:
    """Check a project; `show_sql`: also print the SQL that runs for that model (views inlined,
    the automatic incremental filter placed)."""
    checked = check_project(folder)
    for problem in checked.problems:
        print(problem, file=out)
    if checked.problems:
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


@dataclass(frozen=True, slots=True)
class Selection:
    """A scheduled run: the models to build (dependencies first) and how to record it."""

    models: tuple[str, ...]
    trigger: Literal["freshness", "schedule"]
    full_refresh: frozenset[str] = frozenset()  # recorded as full_refresh (incremental: rebuilt)


def run(
    folder: Path,
    identity: Identity,
    out: IO[str],
    full_refresh: Sequence[str] | None = None,
    selection: Selection | None = None,
) -> int:
    """Build every model, or only the `selection` of a scheduled run. `full_refresh`:
    incremental models to rebuild from every row (all of them when empty; None: none)."""
    checked = check_project(folder)
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
            return _run_locked(project, graph, checked, home, identity, out, refreshed, selection)
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


def _trigger(
    name: str, refreshed: frozenset[str], selection: Selection | None
) -> Literal["manual", "schedule", "freshness", "full_refresh"]:
    if name in refreshed or (selection is not None and name in selection.full_refresh):
        return "full_refresh"
    return "manual" if selection is None else selection.trigger


def _limits() -> Limits:
    return Limits(
        memory_limit=_memory_limit(),
        threads=os.cpu_count() or 2,
        max_concurrent=4,
        queue_size=16,
        default_timeout_ms=DEFAULT_TIMEOUT_MS,
    )


def _location(home: Path) -> LocalLakeLocation:
    return LocalLakeLocation(kind="lake_location", catalog="local", path=str(home / LAKE_FOLDER))


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
    try:
        lake = open_lake(_location(home), _limits(), source_attachments(project.connections))
    except StoreError as error:
        open_error = error
    existing: dict[str, Columns | None] = {}
    previous: dict[str, str | None] = {}
    since: dict[str, str] = {}
    if lake is not None:
        try:
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
        checked.extracted,
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
            lake = open_lake(_location(home), _limits())
        except StoreError as error:
            for problem in failures or [_store_problem(error, ".", "")]:
                print(problem, file=out)
            return 1
    try:
        current: _Outcome | None = None
        try:
            with lake.transaction() as transaction:
                for item, (_, steps) in zip(outcomes, plan, strict=True):
                    if failures:
                        break
                    current = item
                    item.started_at = _now()
                    item.started = True
                    for step in steps:
                        _run_step(transaction, step, item, project.connections)
                    _check_tests(item)
                    item.finished_at = _now()
                    item.outcome = "ran"
                current = None
                if failures:
                    raise _NothingToCommit
        except _NothingToCommit:
            pass
        except (StoreError, _StepFailed) as error:
            for item in outcomes:
                if item.outcome == "ran":
                    item.outcome = "failed"  # rolled back with the run
            if isinstance(error, _StepFailed):
                reason = RunError(code=error.code, message=error.message)
            else:
                reason = RunError(code=error.code, message=_store_message(error))
            if current is not None:
                current.finished_at = _now()
                current.outcome = "failed"
                current.error = reason
                if isinstance(error, _StepFailed) and error.problems:
                    failures = list(error.problems)
                else:
                    failures = [
                        Problem(
                            reason.code,
                            current.model.sql_file,
                            None,
                            f"{current.model.name}: {reason.message}"
                            if isinstance(error, StoreError)
                            else reason.message,
                        )
                    ]
            else:
                # Not a model's fault (the transaction could not start or commit): every
                # model failed for the same reason.
                for item in outcomes:
                    item.outcome = "failed"
                    item.error = reason
                failures = [Problem(reason.code, ".", None, reason.message)]
        warning: Problem | None = None
        if not failures:
            for item in outcomes:
                item.outcome = "success"
            try:
                snapshot = lake.current_snapshot().id
                for item in outcomes:
                    if item.rows_written is None and item.model.config.materialized != "view":
                        item.rows_written = str(_count(lake, item.model, snapshot))
            except StoreError as error:
                # The run is committed: it is recorded as such, without what could not be read.
                warning = _store_problem(error, ".", "after the commit")
        records = [
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
                snapshot_id=snapshot if item.outcome == "success" else None,
                started_at=item.started_at,
                finished_at=item.finished_at,
                outcome=item.outcome,  # type: ignore[arg-type]  # success, failed or not_run
                error=item.error,
            )
            for item in outcomes
        ]
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
        try:
            with lake.transaction() as transaction:
                for statement in record_statements(records):
                    transaction.execute(statement)
        except StoreError as error:
            records_error = error
    finally:
        lake.close()
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
    for item in outcomes:
        if item.model.config.materialized == "view":
            print(f"tested {item.model.name}", file=out)  # a view is not stored
        else:
            print(f"built {item.model.name}: {item.rows_written} rows", file=out)
    return 0


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
                inputs=lineage.inputs(
                    item.model, by_name, project.connections, scope.lake_namespace
                ),
                column_lineage=lineage.column_lineage(
                    item.model,
                    selected or output,
                    by_name,
                    project.connections,
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


def _run_step(
    transaction: Transaction,
    step: Step | Extract | ModelTest | Reconcile,
    item: _Outcome,
    connections: Mapping[str, Connection],
) -> None:
    if isinstance(step, Extract):
        _extract(transaction, step, item, connections)
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


def _extract(
    transaction: Transaction,
    step: Extract,
    item: _Outcome,
    connections: Mapping[str, Connection],
) -> None:
    """Stream an extraction model's rows from ClickHouse into the run's transaction (#98).

    The connection is opened with its usual checks (read-only, limits), the output columns
    must still be those validated, `{since:…}` is bound server-side, and the Arrow stream goes
    straight into DuckDB. A timeout or cancellation closes the response, so ClickHouse cancels
    the query; any failure rolls the run back. The rows read so far are recorded either way.
    """
    connection = connections.get(step.connection)
    assert isinstance(connection, ClickhouseConnection)  # validated before the run
    password = clickhouse.read_password(connection)
    if password is None:
        variable = clickhouse.password_variable(connection)
        raise _StepFailed(
            "PROJECT_SECRET_MISSING",
            f"Environment variable {variable} is not set (the password of connection "
            f"{step.connection}).",
        )
    try:
        source = clickhouse.Source(step.connection, connection, password)
    except clickhouse.SourceError as error:
        raise _StepFailed(error.code, f"{step.model}: {error.message}") from None
    stream: clickhouse.Stream | None = None
    try:
        since = (
            clickhouse.since_value(step.since, step.watermark, step.lookback, source.server_zone)
            if step.since is not None
            else None
        )
        item.since = since
        source.check_columns(step.text, step.since, step.columns)
        stream = source.stream(step.text, step.columns, since, DEFAULT_TIMEOUT_MS // 1000)
        rows = transaction.load_arrow(
            step.schema,
            step.table,
            stream.reader,
            clickhouse.load_columns(step.columns),
            on_interrupt=stream.close,
        )
        item.rows_read = str(rows)
        item.rows_written = item.rows_read
    except clickhouse.SourceError as error:
        raise _StepFailed(error.code, f"{step.model}: {error.message}") from None
    finally:
        if stream is not None:
            if item.rows_read is None:
                item.rows_read = str(stream.rows)
            stream.close()
        source.close()


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


def _count(lake: Lake, model: Model, snapshot: int) -> int:
    request = ReadRequest(
        kind="read",
        sql=f'SELECT count(*) AS n FROM "{model.schema}"."{model.table}"',
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )
    count: int = lake.read(request).read_all().column("n")[0].as_py()
    return count


def runs(folder: Path, identity: Identity, limit: int, out: IO[str]) -> int:
    """Recent runs of a project, newest first, with times in the project's time zone."""
    try:
        project = load_project(folder)
    except ProjectLoadError as error:
        details = error.error.details
        print(Problem(error.code, details.file or ".", details.line, error.error.message), file=out)
        return 1
    home = project.folder / f".{identity.slug}"
    if not (home / LAKE_FOLDER).exists():
        print("no runs yet", file=out)
        return 0
    try:
        with project_lock(home):
            lake = open_lake(_location(home), _limits())
            try:
                snapshot = lake.current_snapshot().id
                request = history_request(snapshot, project.settings.time_zone, limit)
                try:
                    rows = lake.read(request).read_all().to_pylist()
                except StoreError as error:
                    if error.code != "STORE_STATEMENT_INVALID":
                        raise
                    rows = []  # the records table does not exist yet
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
