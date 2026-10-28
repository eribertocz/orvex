"""`validate` and `run`: check a project and build its models into the local lake (P1-09).

The server composes the modules: the project loader (here), model discovery, the dependency
graph and the run plan (`transform`), and the lake (`store`). Transform never opens the lake;
it hands the server a list of statements that run in one write transaction.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.store.v1 import LocalLakeLocation, ReadRequest
from orvex.server.identity import Identity
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.store.errors import StoreError
from orvex.store.lake import Limits, open_lake
from orvex.transform.graph import Graph, build_graph, plan_run
from orvex.transform.models import Discovery, Model, discover_models
from orvex.transform.sources import source_attachments

LOCK_FILE = "lake.lock"
LAKE_FOLDER = "lake"
DEFAULT_MEMORY = "4GB"
DEFAULT_TIMEOUT_MS = 3_600_000


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
            return _run_locked(project, graph, checked.discovery, home, out)
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


def _run_locked(
    project: Project, graph: Graph, discovery: Discovery, home: Path, out: IO[str]
) -> int:
    plan = plan_run(discovery.models, graph)
    location = LocalLakeLocation(
        kind="lake_location", catalog="local", path=str(home / LAKE_FOLDER)
    )
    limits = Limits(
        memory_limit=_memory_limit(),
        threads=os.cpu_count() or 2,
        max_concurrent=4,
        queue_size=16,
        default_timeout_ms=DEFAULT_TIMEOUT_MS,
    )
    current = ""
    try:
        lake = open_lake(location, limits, source_attachments(project.connections))
        try:
            with lake.transaction() as transaction:
                for model, statements in plan:
                    current = model.name
                    for statement in statements:
                        transaction.execute(statement)
            current = ""
            snapshot = lake.current_snapshot().id
            for model, _ in plan:
                request = ReadRequest(
                    kind="read",
                    sql=f'SELECT count(*) AS n FROM "{model.schema}"."{model.table}"',
                    parameters=[],
                    snapshot=snapshot,
                    batch_size=1,
                )
                count = lake.read(request).read_all().column("n")[0].as_py()
                print(f"built {model.name}: {count} rows", file=out)
        finally:
            lake.close()
    except StoreError as error:
        where = next((m.sql_file for m, _ in plan if m.name == current), ".")
        prefix = f"{current}: " if current else ""
        detail = error.details.get("duckdb", "")
        message = f"{prefix}{error.message}" + (f" {detail}" if detail else "")
        print(Problem(error.code, where, None, message), file=out)
        return 1
    print(f"snapshot {snapshot}", file=out)
    return 0
