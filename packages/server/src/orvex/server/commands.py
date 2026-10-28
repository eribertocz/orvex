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
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.run_record.v1 import ModuleVersion, RowsWritten, RunError, RunRecord, Versions
from orvex.contracts.store.v1 import LocalLakeLocation, ReadRequest
from orvex.server.identity import Identity
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.server.versions import module_versions
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake
from orvex.transform.graph import Graph, build_graph, plan_run
from orvex.transform.models import Discovery, Model, discover_models
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
    return Checked(project, discovery, graph, tuple(problems))


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
    if model.config.materialized == "incremental":
        return Problem(
            "UNSUPPORTED_MODEL_KIND",
            model.config_file or model.sql_file,
            None,
            f"{model.name}: incremental models run from a later release (P1-11).",
        )
    return None


def run(folder: Path, identity: Identity, out: IO[str]) -> int:
    checked = check_project(folder)
    if checked.problems or checked.project is None or checked.graph is None:
        for problem in checked.problems:
            print(problem, file=out)
        return 1
    project, graph = checked.project, checked.graph
    refused = [p for p in map(_not_runnable, checked.discovery.models) if p is not None]
    if refused:
        for problem in refused:
            print(problem, file=out)
        return 1
    home = project.folder / f".{identity.slug}"
    try:
        with project_lock(home):
            return _run_locked(project, graph, checked.discovery, home, identity, out)
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
            ["git", "-C", str(folder), *arguments],
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
    rows_written: str | None = None
    error: RunError | None = None


def _run_locked(
    project: Project,
    graph: Graph,
    discovery: Discovery,
    home: Path,
    identity: Identity,
    out: IO[str],
) -> int:
    plan = plan_run(discovery.models, graph)
    run_id = str(uuid.uuid4())
    print(f"run {run_id}", file=out)
    commit, dirty = _project_commit(project.folder)
    started = _now()
    outcomes = [_Outcome(model, started, started) for model, _ in plan]
    snapshot: int | None = None
    failure: Problem | None = None
    lake = None
    try:
        lake = open_lake(_location(home), _limits(), source_attachments(project.connections))
    except StoreError as error:
        failure = _store_problem(error, ".", "")
        for item in outcomes:
            item.outcome = "failed"
            item.error = RunError(code=error.code, message=error.message)
    if lake is None:
        # The run could not start (a source is unreachable, say): it is still recorded, in a
        # lake opened without sources.
        try:
            lake = open_lake(_location(home), _limits())
        except StoreError as error:
            print(failure or _store_problem(error, ".", ""), file=out)
            return 1
    try:
        current: _Outcome | None = None
        try:
            with lake.transaction() as transaction:
                for item, (_, statements) in zip(outcomes, plan, strict=True):
                    if failure is not None:
                        break
                    current = item
                    item.started_at = _now()
                    for statement in statements:
                        transaction.execute(statement)
                    item.finished_at = _now()
                    item.outcome = "ran"
                current = None
                if failure is not None:
                    raise _NothingToCommit
        except _NothingToCommit:
            pass
        except StoreError as error:
            for item in outcomes:
                if item.outcome == "ran":
                    item.outcome = "failed"  # rolled back with the run
            if current is not None:
                current.finished_at = _now()
                current.outcome = "failed"
                current.error = RunError(code=error.code, message=_store_message(error))
                failure = _store_problem(error, current.model.sql_file, current.model.name)
            else:
                failure = _store_problem(error, ".", "")
        if failure is None:
            snapshot = lake.current_snapshot().id
            for item in outcomes:
                item.outcome = "success"
                item.rows_written = str(_count(lake, item.model, snapshot))
        records = [
            RunRecord(
                run_id=run_id,
                model=item.model.name,
                trigger="manual",
                materialized=item.model.config.materialized or "table",
                project_commit=commit,
                project_dirty=dirty,
                versions=_versions(identity),
                since=None,
                rows_read=None,
                rows_written=None if item.rows_written is None else RowsWritten(item.rows_written),
                watermark=None,
                tests=[],
                reconciliation=[],
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
            print(failure or _store_problem(error, ".", "run records"), file=out)
            return 1
    finally:
        lake.close()
    if failure is not None:
        print(failure, file=out)
        return 1
    print(f"snapshot {snapshot}", file=out)
    for item in outcomes:
        print(f"built {item.model.name}: {item.rows_written} rows", file=out)
    return 0


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
