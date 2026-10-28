"""`validate` and `run`: check a project and build its models into the local lake (P1-09).

The server composes the modules: the project loader (here), model discovery, the dependency
graph and the run plan (`transform`), and the lake (`store`). Transform never opens the lake;
it hands the server a list of statements that run in one write transaction.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from orvex.contracts.model_config.v1 import ModelError
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
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.server.versions import module_versions
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, Transaction, open_lake
from orvex.transform import incremental, model_tests, reconciliation
from orvex.transform.graph import Graph, build_graph, compiled_sql, plan_run, test_relations
from orvex.transform.incremental import Columns, Count, Refuse, Step, Watermark
from orvex.transform.model_tests import ModelTest
from orvex.transform.models import Discovery, Model, discover_models
from orvex.transform.reconciliation import Reconcile
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
    differences: Mapping[str, list[str]] = field(default_factory=dict)


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
    for model in discovery.models:
        problems += [_from_model_error(item) for item in incremental.config_problems(model)]
        problems += [_from_model_error(item) for item in reconciliation.problems(model, kinds)]
    columns: dict[str, Columns] = {}
    differences: dict[str, list[str]] = {}
    needs_binding = any(
        m.config.materialized == "incremental" or m.config.tests or m.config.reconciliation
        for m in discovery.models
    )
    if needs_binding and not problems:
        columns, differences, bind_problems = _bind(project, discovery, graph)
        problems += bind_problems
        problems += [
            _from_model_error(item) for item in incremental.problems_for(discovery.models, columns)
        ]
    return Checked(project, discovery, graph, tuple(problems), columns, differences)


def _bind(
    project: Project, discovery: Discovery, graph: Graph
) -> tuple[dict[str, Columns], dict[str, list[str]], list[Problem]]:
    """Output columns of every model, from DuckDB's binder; nothing is executed.

    Binding happens in a scratch lake in a temporary folder (never the project's): upstream
    models are stand-in empty tables with the columns found for them, and sources are attached
    as for a run.
    """
    by_name = {model.name: model for model in discovery.models}
    relations = test_relations(discovery.models)
    columns: dict[str, Columns] = {}
    differences: dict[str, list[str]] = {}
    problems: list[Problem] = []
    failed: set[str] = set()
    scratch = Path(tempfile.mkdtemp(prefix="bind-"))
    try:
        location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(scratch))
        try:
            lake = open_lake(location, _limits(), source_attachments(project.connections))
        except StoreError as error:
            return {}, {}, [_store_problem(error, ".", "")]
        try:
            for name in graph.order:
                model = by_name[name]
                if model.extraction or failed & set(graph.dependencies.get(name, ())):
                    failed.add(name)
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
    return columns, differences, problems


def _bind_reconciliation(
    lake: Lake, model: Model, relation: str
) -> tuple[list[Problem], list[str]]:
    """Each check bound on the model and its source: the type of its measure difference, which
    must be an exact number (integers or DECIMAL, law 11)."""
    file = model.config_file or model.sql_file
    problems: list[Problem] = []
    kinds: list[str] = []
    for index, check in enumerate(model.config.reconciliation or []):
        pointer = f"/reconciliation/{index}"
        try:
            ((_, kind),) = lake.describe(reconciliation.difference_sql(check, relation))
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
        if not reconciliation.exact_difference(kind):
            problems.append(
                Problem(
                    "MODEL_CONFIG_INVALID",
                    file,
                    None,
                    f"{file}: {pointer}/measure: {check.name} compares {kind} values; use a "
                    "measure with an exact type (integer or DECIMAL) — floats are not "
                    "compared (law 11).",
                )
            )
            continue
        kinds.append(kind)
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


def validate(folder: Path, out: IO[str]) -> int:
    checked = check_project(folder)
    for problem in checked.problems:
        print(problem, file=out)
    if checked.problems:
        return 1
    print(f"{len(checked.discovery.models)} models, no problems.", file=out)
    return 0


def _not_runnable(model: Model) -> Problem | None:
    if model.extraction:
        return Problem(
            "UNSUPPORTED_MODEL_KIND",
            model.sql_file,
            None,
            f"{model.name}: extraction models run from a later release (P1-17).",
        )
    return None


def run(
    folder: Path, identity: Identity, out: IO[str], full_refresh: Sequence[str] | None = None
) -> int:
    """Build every model. `full_refresh`: incremental models to rebuild from every row (all of
    them when empty; None: no full refresh)."""
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
    refused = [p for p in map(_not_runnable, checked.discovery.models) if p is not None]
    if refused:
        for problem in refused:
            print(problem, file=out)
        return 1
    home = project.folder / f".{identity.slug}"
    try:
        with project_lock(home):
            return _run_locked(project, graph, checked, home, identity, out, refreshed)
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


def _run_locked(
    project: Project,
    graph: Graph,
    checked: Checked,
    home: Path,
    identity: Identity,
    out: IO[str],
    refreshed: frozenset[str] = frozenset(),
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
    )
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
                    for step in steps:
                        _run_step(transaction, step, item)
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
                trigger="full_refresh" if item.model.name in refreshed else "manual",
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
        try:
            with lake.transaction() as transaction:
                for statement in record_statements(records):
                    transaction.execute(statement)
        except StoreError as error:
            for problem in failures:
                print(problem, file=out)
            print(_store_problem(error, ".", "run records were not written"), file=out)
            return 1
    finally:
        lake.close()
    if failures:
        for problem in failures:
            print(problem, file=out)
        return 1
    if warning is not None:
        print(warning, file=out)
    print(f"snapshot {snapshot if snapshot is not None else '-'}", file=out)
    for item in outcomes:
        if item.model.config.materialized == "view":
            print(f"tested {item.model.name}", file=out)  # a view is not stored
        else:
            print(f"built {item.model.name}: {item.rows_written} rows", file=out)
    return 0


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
    if isinstance(step, ModelTest | Reconcile):
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
    else:
        value = transaction.value(StatementRequest(kind="statement", sql=step.sql, parameters=[]))
        if isinstance(step, Count):
            item.rows_read = value or "0"
            item.rows_written = item.rows_read
        elif value not in {None, "0"}:
            raise _StepFailed(step.code, step.message)


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
