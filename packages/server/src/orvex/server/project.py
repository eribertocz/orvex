"""Load a project folder: `project.yaml` and `connections.yaml` (contract `project` v1, P1-05).

The files are plain YAML the Builder edits by hand. Secrets stay references (`${env:NAME}`):
the loader never reads them, `resolve_secret` reads one when a connection is used, and no error
or object of this module ever contains a secret value.
"""

from __future__ import annotations

import os
import re
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
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
from pydantic import BaseModel, ValidationError

PROJECT_FILE = "project.yaml"
CONNECTIONS_FILE = "connections.yaml"
SUPPORTED_VERSION = 1
RESERVED_CONNECTION = "lake"

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
    folder = Path(folder).expanduser().absolute()
    project_path = folder / PROJECT_FILE
    if not project_path.is_file():
        raise ProjectLoadError(
            "PROJECT_NOT_FOUND",
            f"{PROJECT_FILE} not found in {folder}.",
            file=PROJECT_FILE,
        )
    document = _Document(project_path)
    settings = document.model(ProjectFile, document.data, "")
    # The generated model admits null for fields with a default; the contract does not.
    raw = document.data if isinstance(document.data, dict) else {}
    for key in ("locale", "retention"):
        if key in raw and raw[key] is None:
            raise document.schema_error(f"'{key}' cannot be null.", f"/{key}")
    retention = raw.get("retention")
    if isinstance(retention, dict) and "snapshots" in retention and retention["snapshots"] is None:
        raise document.schema_error("'snapshots' cannot be null.", "/retention/snapshots")
    zone = settings.time_zone
    try:
        zoneinfo.ZoneInfo(zone)
    except zoneinfo.ZoneInfoNotFoundError, ValueError:
        raise document.schema_error(
            f"time_zone '{zone}' is not an IANA time zone name.", "/time_zone"
        ) from None
    if settings.retention is None:
        settings = settings.model_copy(update={"retention": Retention()})

    connections: dict[str, Connection] = {}
    connections_path = folder / CONNECTIONS_FILE
    if connections_path.exists():
        connections = _load_connections(_Document(connections_path), folder)
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


def _load_connections(document: _Document, folder: Path) -> dict[str, Connection]:
    data = document.data
    if not isinstance(data, dict):
        raise document.schema_error("The file must be a mapping.", "")
    unknown = sorted(set(data) - {"version", "connections"})
    if unknown:
        raise document.schema_error(f"Unknown key '{unknown[0]}'.", f"/{unknown[0]}")
    if "connections" not in data:
        raise document.schema_error("'connections' is required.", "")
    entries = data["connections"]
    if not isinstance(entries, dict):
        raise document.schema_error("'connections' must be a mapping.", "/connections")
    connections: dict[str, Connection] = {}
    for name, entry in entries.items():
        pointer = f"/connections/{name}"
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
        connection = document.model(model, entry, pointer)
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
        if isinstance(connection, FILE_TYPES):
            connection = connection.model_copy(
                update={"path": type(connection.path)(_absolute(connection.path.root, folder))}
            )
        connections[name] = connection
    return connections


def _absolute(path: str, folder: Path) -> str:
    expanded = Path(os.path.expanduser(path))
    return str(expanded if expanded.is_absolute() else folder / expanded)


class _Document:
    """One YAML file: its data, and where each value is in the text."""

    def __init__(self, path: Path) -> None:
        self.file = path.name
        text = path.read_text(encoding="utf-8")
        try:
            self._root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.MarkedYAMLError as error:
            mark = error.problem_mark or error.context_mark
            raise ProjectLoadError(
                "PROJECT_YAML_INVALID",
                f"{self.file}: {error.problem or error.context or 'invalid YAML'}.",
                file=self.file,
                line=None if mark is None else mark.line + 1,
            ) from None
        except yaml.YAMLError as error:
            raise ProjectLoadError(
                "PROJECT_YAML_INVALID", f"{self.file}: {type(error).__name__}.", file=self.file
            ) from None
        self._check_duplicates(self._root, "")
        loader = yaml.SafeLoader("")
        try:
            self.data: object = (
                None if self._root is None else loader.construct_document(self._root)
            )
        except yaml.MarkedYAMLError as error:
            mark = error.problem_mark
            raise ProjectLoadError(
                "PROJECT_YAML_INVALID",
                f"{self.file}: {error.problem or 'invalid value'}.",
                file=self.file,
                line=None if mark is None else mark.line + 1,
            ) from None
        finally:
            loader.dispose()
        version = self.data.get("version") if isinstance(self.data, dict) else None
        if isinstance(version, int) and not isinstance(version, bool) and version != 1:
            raise ProjectLoadError(
                "PROJECT_VERSION_UNSUPPORTED",
                f"{self.file}: version {version} is not supported; this release reads "
                f"version {SUPPORTED_VERSION}.",
                file=self.file,
                path="/version",
                line=self.line("/version"),
            )

    def _check_duplicates(self, node: yaml.Node | None, pointer: str) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set[object] = set()
            for key, value in node.value:
                name = key.value if isinstance(key, yaml.ScalarNode) else None
                if name in seen:
                    code = (
                        "PROJECT_SCHEMA_INVALID"
                        if pointer == "/connections"
                        else ("PROJECT_YAML_INVALID")
                    )
                    what = "connection" if code == "PROJECT_SCHEMA_INVALID" else "key"
                    raise ProjectLoadError(
                        code,
                        f"{self.file}: duplicate {what} '{name}'.",
                        file=self.file,
                        path=f"{pointer}/{name}",
                        line=key.start_mark.line + 1,
                    )
                seen.add(name)
                self._check_duplicates(value, f"{pointer}/{name}")
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self._check_duplicates(item, f"{pointer}/{index}")

    def line(self, pointer: str) -> int | None:
        """The line of the value at a JSON pointer, or of its closest existing parent."""
        node = self._root
        if node is None:
            return None
        for part in [part for part in pointer.split("/") if part]:
            child = None
            if isinstance(node, yaml.MappingNode):
                child = next((v for k, v in node.value if k.value == part), None)
            elif isinstance(node, yaml.SequenceNode) and part.isdigit():
                index = int(part)
                child = node.value[index] if index < len(node.value) else None
            if child is None:
                break
            node = child
        line: int = node.start_mark.line
        return line + 1

    def schema_error(self, message: str, pointer: str) -> ProjectLoadError:
        return ProjectLoadError(
            "PROJECT_SCHEMA_INVALID",
            f"{self.file}: {message}",
            file=self.file,
            path=pointer,
            line=self.line(pointer),
        )

    def model[M: BaseModel](self, model: type[M], data: object, pointer: str) -> M:
        """Validate strictly; the error names the location, never the value found there."""
        if not isinstance(data, dict):
            raise self.schema_error("Expected a mapping.", pointer)
        try:
            return model.model_validate(data, strict=True, by_alias=True)
        except ValidationError as error:
            first = error.errors(include_input=False, include_url=False)[0]
            location = "/".join(str(part) for part in first["loc"] if part != "root")
            where = f"{pointer}/{location}" if location else pointer
            if first["type"] == "extra_forbidden":
                message = f"Unknown key '{first['loc'][-1]}'."
            elif first["loc"] and first["loc"][0] == "password":
                message = f"{where}: a password must be a reference ${{env:NAME}}, never a value."
            else:
                message = f"{where or '/'}: {first['msg']}."
            raise self.schema_error(message, where) from None
