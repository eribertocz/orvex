"""The instance directory and the project registry of a local installation."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orvex.server.identity import Identity

REGISTRY_FILE = "server.sqlite"


class ProjectPathError(ValueError):
    """The path given for a project cannot be a project folder."""

    code = "PROJECT_PATH_NOT_A_DIRECTORY"

    def __init__(self, path: Path) -> None:
        super().__init__(f"{self.code}: {path} exists and is not a directory")
        self.path = path


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    path: Path
    last_opened: str
    in_memory: bool


def home_variable(identity: Identity) -> str:
    """`<SLUG>_HOME`, derived from the identity slug."""
    return f"{identity.slug.upper().replace('-', '_')}_HOME"


def instance_directory(identity: Identity, environment: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    override = environment.get(home_variable(identity))
    directory = Path(override) if override else Path.home() / f".{identity.slug}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class ProjectRegistry:
    """Projects known to this instance, in one SQLite file in the instance directory."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._database = directory / REGISTRY_FILE
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS projects ("
                " id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, last_opened TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, path: Path) -> Project:
        resolved = str(path.resolve())
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO projects (id, path, last_opened) VALUES (?, ?, ?)"
                " ON CONFLICT (path) DO UPDATE SET last_opened = excluded.last_opened",
                (uuid.uuid4().hex, resolved, now),
            )
            row = connection.execute(
                "SELECT id, path, last_opened FROM projects WHERE path = ?", (resolved,)
            ).fetchone()
        return Project(row[0], Path(row[1]), row[2], in_memory=False)

    def projects(self) -> list[Project]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, path, last_opened FROM projects ORDER BY path"
            ).fetchall()
        return [Project(row[0], Path(row[1]), row[2], in_memory=False) for row in rows]


def open_project(path: Path, registry: ProjectRegistry) -> Project:
    """Open the folder, creating it when it does not exist, and register it."""
    if path.exists() and not path.is_dir():
        raise ProjectPathError(path)
    path.mkdir(parents=True, exist_ok=True)
    return registry.register(path)


@contextmanager
def in_memory_project(identity: Identity) -> Iterator[Project]:
    """A temporary project folder that is deleted when the context ends."""
    with tempfile.TemporaryDirectory(prefix=f"{identity.slug}-") as directory:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        yield Project(uuid.uuid4().hex, Path(directory), now, in_memory=True)
