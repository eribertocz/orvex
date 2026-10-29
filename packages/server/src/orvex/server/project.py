"""Load a project folder: `project.yaml` and `connections.yaml` (contract `project` v1, P1-05).

The files are plain YAML the Builder edits by hand. Secrets stay references (`${env:NAME}`):
the loader never reads them, `resolve_secret` reads one when a connection is used, and no error
or object of this module ever contains a secret value.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    ProjectError,
    ProjectErrorDetails,
    ProjectFile,
    Retention,
    SecretReference,
    SqliteConnection,
)
from orvex.transform.config_file import ConfigFile, ConfigProblem, token

PROJECT_FILE = "project.yaml"
CONNECTIONS_FILE = "connections.yaml"
SUPPORTED_VERSION = 1
RESERVED_CONNECTION = "lake"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})  # the local machine (#120, #122)


def is_local_host(host: str) -> bool:
    """Whether `host` is the local machine: `localhost`, `127.0.0.1`, `::1` or a Unix-socket
    directory (a path starting with `/`). libpq reads `host` as a comma-separated list and tries
    each entry, so every entry must be local."""
    return all(
        entry.strip().startswith("/") or entry.strip().lower() in LOCAL_HOSTS
        for entry in host.split(",")
    )


type Connection = (
    ParquetConnection
    | CsvConnection
    | DuckdbConnection
    | SqliteConnection
    | PostgresConnection
    | ClickhouseConnection
)
DATABASE_TYPES = (DuckdbConnection, SqliteConnection, PostgresConnection, ClickhouseConnection)
FILE_TYPES = (ParquetConnection, CsvConnection, DuckdbConnection, SqliteConnection)

CONNECTION_TYPES: dict[str, type[Connection]] = {
    "parquet": ParquetConnection,
    "csv": CsvConnection,
    "duckdb": DuckdbConnection,
    "sqlite": SqliteConnection,
    "postgres": PostgresConnection,
    "clickhouse": ClickhouseConnection,
}
CONNECTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
SECRET_REFERENCE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")


class ProjectLoadError(Exception):
    """A project that cannot be loaded, or a secret that cannot be resolved."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file: str | None = None,
        path: str | None = None,
        line: int | None = None,
        connection: str | None = None,
        variable: str | None = None,
    ) -> None:
        self.error = ProjectError(
            code=code,  # type: ignore[arg-type]  # validated against the contract's codes
            message=message,
            details=ProjectErrorDetails(
                file=file, path=path, line=line, connection=connection, variable=variable
            ),
        )
        super().__init__(f"{code}: {message}")

    @property
    def code(self) -> str:
        return self.error.code.root


@dataclass(frozen=True, slots=True)
class Project:
    """A loaded project: its settings (defaults applied) and its connections by name.

    Connection paths are absolute; passwords are the `${env:NAME}` references of the files.
    """

    folder: Path
    settings: ProjectFile
    connections: Mapping[str, Connection]


def load_project(folder: Path) -> Project:
    problem: ConfigProblem | None = None
    try:
        return _load_project(folder)
    except ConfigProblem as raised:
        problem = raised
    # Raised outside the handler, so the problem is not attached as context.
    raise _project_error(problem)


def _project_error(problem: ConfigProblem) -> ProjectLoadError:
    """The project error code for a configuration-file problem."""
    path = problem.path or ""
    if problem.duplicate and path.count("/") == 2 and path.startswith("/connections/"):
        name = path.rsplit("/", 1)[1]
        return ProjectLoadError(
            "PROJECT_SCHEMA_INVALID",
            f"{problem.file}: duplicate connection '{name}'.",
            file=problem.file,
            path=path,
            line=problem.line,
        )
    codes = {
        "unreadable": "PROJECT_NOT_FOUND",
        "yaml": "PROJECT_YAML_INVALID",
        "schema": "PROJECT_SCHEMA_INVALID",
        "version": "PROJECT_VERSION_UNSUPPORTED",
    }
    return ProjectLoadError(
        codes[problem.kind],
        problem.message,
        file=problem.file,
        path=problem.path,
        line=problem.line,
    )


def _load_project(folder: Path) -> Project:
    folder = Path(folder).expanduser().absolute()
    project_path = folder / PROJECT_FILE
    if not project_path.is_file():
        raise ProjectLoadError(
            "PROJECT_NOT_FOUND",
            f"{PROJECT_FILE} not found in {folder}.",
            file=PROJECT_FILE,
        )
    document = ConfigFile(project_path, SUPPORTED_VERSION)
    settings = document.model(ProjectFile, document.data, "")
    zone = settings.time_zone
    if zone not in _iana_zones():
        raise document.schema_error(
            f"time_zone '{zone}' is not an IANA time zone name.", "/time_zone"
        )
    if settings.retention is None:
        settings = settings.model_copy(update={"retention": Retention()})

    connections: dict[str, Connection] = {}
    connections_path = folder / CONNECTIONS_FILE
    if connections_path.exists() or connections_path.is_symlink():  # a broken link is reported
        connections = _load_connections(ConfigFile(connections_path, SUPPORTED_VERSION), folder)
    return Project(folder=folder, settings=settings, connections=connections)


def resolve_secret(reference: SecretReference | str) -> str:
    """The value of a `${env:NAME}` reference, read now from the environment."""
    text = reference.root if isinstance(reference, SecretReference) else reference
    match = SECRET_REFERENCE.fullmatch(text)
    if match is None:
        raise ProjectLoadError(
            "PROJECT_SCHEMA_INVALID", "A secret must be a reference of the form ${env:NAME}."
        )
    variable = match.group(1)
    value = os.environ.get(variable)
    if value is None:
        raise ProjectLoadError(
            "PROJECT_SECRET_MISSING",
            f"Environment variable {variable} is not set.",
            variable=variable,
        )
    return value


def _load_connections(document: ConfigFile, folder: Path) -> dict[str, Connection]:
    data = document.data
    if not isinstance(data, dict):
        raise document.schema_error("The file must be a mapping.", "")
    unknown = sorted(set(data) - {"version", "connections"})
    if unknown:
        raise document.schema_error(f"Unknown key '{unknown[0]}'.", f"/{token(unknown[0])}")
    if "connections" not in data:
        raise document.schema_error("'connections' is required.", "")
    entries = data["connections"]
    if not isinstance(entries, dict):
        raise document.schema_error("'connections' must be a mapping.", "/connections")
    connections: dict[str, Connection] = {}
    for name, entry in entries.items():
        pointer = f"/connections/{token(name)}"
        if not isinstance(name, str) or not CONNECTION_NAME.fullmatch(name):
            raise document.schema_error(
                f"Connection name '{name}' must be a lowercase identifier "
                "(a letter, then letters, digits or _).",
                pointer,
            )
        if name == RESERVED_CONNECTION:
            raise document.schema_error(
                f"'{RESERVED_CONNECTION}' is reserved for the project's own data.", pointer
            )
        kind = entry.get("type") if isinstance(entry, dict) else None
        model = CONNECTION_TYPES.get(kind) if isinstance(kind, str) else None
        if model is None:
            raise document.schema_error(
                f"Connection '{name}' needs a type: one of {', '.join(CONNECTION_TYPES)}.",
                f"{pointer}/type",
            )
        connection = document.model(model, entry, pointer, secret_fields=frozenset({"password"}))
        if isinstance(connection, DATABASE_TYPES) and not connection.read_only.root:
            raise ProjectLoadError(
                "SOURCE_NOT_READ_ONLY",
                f"Connection '{name}' must declare read_only: true; every database source is "
                "read-only.",
                file=document.file,
                path=f"{pointer}/read_only",
                line=document.line(f"{pointer}/read_only"),
                connection=name,
            )
        if (
            isinstance(connection, PostgresConnection)
            and not is_local_host(connection.host)
            and connection.ssl_mode != "verify-full"
        ):
            raise ProjectLoadError(
                "SOURCE_TLS_REQUIRED",
                f"Connection '{name}' is on another machine ({connection.host}) and must "
                "declare ssl_mode: verify-full, so its password and rows travel only over TLS "
                "that checks the server's certificate and name.",
                file=document.file,
                path=f"{pointer}/ssl_mode",
                line=document.line(f"{pointer}/ssl_mode"),
                connection=name,
            )
        if isinstance(connection, FILE_TYPES):
            connection = connection.model_copy(
                update={"path": type(connection.path)(_absolute(connection.path.root, folder))}
            )
        connections[name] = connection
    return connections


@functools.cache
def _iana_zones() -> frozenset[str]:
    """The IANA zone names of the bundled database: the same on every platform, without
    system-only entries such as `localtime` (the machine's own zone)."""
    text = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    # `Factory` is a placeholder of the database, not a place.
    return frozenset(text.split()) - {"Factory"}


def _absolute(path: str, folder: Path) -> str:
    expanded = Path(os.path.expanduser(path))
    return str(expanded if expanded.is_absolute() else folder / expanded)
