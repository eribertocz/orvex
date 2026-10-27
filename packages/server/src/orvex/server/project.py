"""Load a project folder: `project.yaml` and `connections.yaml` (contract `project` v1, P1-05).

The files are plain YAML the Builder edits by hand. Secrets stay references (`${env:NAME}`):
the loader never reads them, `resolve_secret` reads one when a connection is used, and no error
or object of this module ever contains a secret value.
"""

from __future__ import annotations

import functools
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
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
# Hand-written configuration is small and shallow; larger or deeper files are refused before
# they can exhaust memory or the parser's stack.
MAX_FILE_BYTES = 1024 * 1024
MAX_DEPTH = 32

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
        raise document.schema_error(f"Unknown key '{unknown[0]}'.", f"/{_token(unknown[0])}")
    if "connections" not in data:
        raise document.schema_error("'connections' is required.", "")
    entries = data["connections"]
    if not isinstance(entries, dict):
        raise document.schema_error("'connections' must be a mapping.", "/connections")
    connections: dict[str, Connection] = {}
    for name, entry in entries.items():
        pointer = f"/connections/{_token(name)}"
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


def _first_null(value: object, pointer: str) -> str | None:
    """The JSON pointer of the first null value, or None (the root itself is left to callers)."""
    items: list[tuple[str, object]] = []
    if isinstance(value, dict):
        items = [(f"{pointer}/{_token(key)}", child) for key, child in value.items()]
    elif isinstance(value, list):
        items = [(f"{pointer}/{index}", child) for index, child in enumerate(value)]
    for child_pointer, child in items:
        if child is None:
            return child_pointer
        found = _first_null(child, child_pointer)
        if found is not None:
            return found
    return None


def _token(key: object) -> str:
    """A key as a JSON pointer reference token (RFC 6901)."""
    text = "null" if key is None else str(key).lower() if isinstance(key, bool) else str(key)
    return text.replace("~", "~0").replace("/", "~1")


def _untoken(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


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


class _Document:
    """One YAML file: its data, and where each value is in the text."""

    def __init__(self, path: Path) -> None:
        self.file = path.name
        # Errors are raised outside the handlers: a YAML error quotes the offending line, which
        # may hold a secret, and must not stay attached to ours as its context.
        failure: ProjectLoadError | None = None
        data = b""
        try:
            # Only a regular file: a pipe would block and a device could be endless. The read is
            # bounded too, so the limit never depends on the size the file system reports.
            # Opened without blocking (a pipe), then checked on the open file itself.
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                regular = stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
                if regular:
                    data = handle.read(MAX_FILE_BYTES + 1)
            if not regular:
                failure = ProjectLoadError(
                    "PROJECT_NOT_FOUND", f"{self.file} is not a regular file.", file=self.file
                )
            elif len(data) > MAX_FILE_BYTES:
                failure = ProjectLoadError(
                    "PROJECT_YAML_INVALID",
                    f"{self.file}: larger than {MAX_FILE_BYTES // 1024} KiB.",
                    file=self.file,
                )
        except OSError as error:
            failure = ProjectLoadError(
                "PROJECT_NOT_FOUND",
                f"{self.file} cannot be read ({error.strerror or type(error).__name__}).",
                file=self.file,
            )
        if failure is not None:
            raise failure
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            # The decode error carries the whole file; only the position is kept.
            failure = ProjectLoadError(
                "PROJECT_YAML_INVALID",
                f"{self.file}: the file is not UTF-8 text (byte {error.start}).",
                file=self.file,
            )
        if failure is not None:
            raise failure
        try:
            self._root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as error:
            failure = self._yaml_failure(error)
        except RecursionError:
            failure = self._too_deep(None)
        if failure is not None:
            raise failure
        self._check_duplicates(self._root, "", set(), 0)
        loader = yaml.SafeLoader("")
        try:
            self.data: object = (
                None if self._root is None else loader.construct_document(self._root)
            )
        except yaml.YAMLError as error:
            failure = self._yaml_failure(error)
        except RecursionError:
            failure = self._too_deep(None)
        except (ValueError, OverflowError) as error:
            # A scalar Python cannot build: an integer over 4300 digits, an impossible date.
            failure = ProjectLoadError(
                "PROJECT_YAML_INVALID",
                f"{self.file}: a value cannot be read ({type(error).__name__}).",
                file=self.file,
            )
        finally:
            loader.dispose()
        if failure is not None:
            raise failure
        null = _first_null(self.data, "")
        if null is not None:
            # No field of these files is nullable; the generated models would admit null where
            # a field has a default (TLS `secure`, for example).
            raise self.schema_error(f"{null or '/'}: a value is required, not null.", null)
        if not isinstance(self.data, dict):
            return
        if "version" not in self.data:
            raise self.schema_error("'version' is required.", "")
        version = self.data["version"]
        if type(version) is not int:  # a YAML `true` is a bool, which Python counts as 1
            raise self.schema_error("'version' must be an integer.", "/version")
        if version != SUPPORTED_VERSION:
            raise ProjectLoadError(
                "PROJECT_VERSION_UNSUPPORTED",
                f"{self.file}: version {version} is not supported; this release reads "
                f"version {SUPPORTED_VERSION}.",
                file=self.file,
                path="/version",
                line=self.line("/version"),
            )

    def _yaml_failure(self, error: yaml.YAMLError) -> ProjectLoadError:
        """The problem and its line, without the quoted text of the line."""
        problem: str | None = None
        line: int | None = None
        if isinstance(error, yaml.MarkedYAMLError):
            problem = error.problem or error.context
            mark = error.problem_mark or error.context_mark
            line = None if mark is None else mark.line + 1
        return ProjectLoadError(
            "PROJECT_YAML_INVALID",
            f"{self.file}: {problem or 'invalid YAML'}.",
            file=self.file,
            line=line,
        )

    def _too_deep(self, line: int | None) -> ProjectLoadError:
        return ProjectLoadError(
            "PROJECT_YAML_INVALID",
            f"{self.file}: values are nested more than {MAX_DEPTH} levels deep.",
            file=self.file,
            line=line,
        )

    def _check_duplicates(
        self, node: yaml.Node | None, pointer: str, seen_nodes: set[int], depth: int
    ) -> None:
        """Refuse duplicate keys, and anchors and aliases.

        An alias reuses a node already seen; refusing it rules out self-references and the
        exponential expansion of nested aliases, and keeps every value at one place in the file.
        """
        if node is None:
            return
        if id(node) in seen_nodes:
            raise ProjectLoadError(
                "PROJECT_YAML_INVALID",
                f"{self.file}: anchors and aliases (&, *) are not supported; write each value "
                "where it is used.",
                file=self.file,
                path=pointer,
                line=node.start_mark.line + 1,
            )
        seen_nodes.add(id(node))
        if depth > MAX_DEPTH:
            raise self._too_deep(node.start_mark.line + 1)
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
                        path=f"{pointer}/{_token(name)}",
                        line=key.start_mark.line + 1,
                    )
                seen.add(name)
                self._check_duplicates(value, f"{pointer}/{_token(name)}", seen_nodes, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self._check_duplicates(item, f"{pointer}/{index}", seen_nodes, depth + 1)

    def line(self, pointer: str) -> int | None:
        """The line of the value at a JSON pointer, or of its closest existing parent."""
        node = self._root
        if node is None:
            return None
        for part in [_untoken(part) for part in pointer.split("/")[1:]]:
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
        # Raised outside the handler: the validation error quotes the input (a literal password,
        # for example), and it must not stay attached to ours as its context.
        location = "/".join(_token(part) for part in first["loc"] if part != "root")
        where = f"{pointer}/{location}" if location else pointer
        if first["type"] == "extra_forbidden":
            message = f"Unknown key '{first['loc'][-1]}'."
        elif first["loc"] and first["loc"][0] == "password":
            message = f"{where}: a password must be a reference ${{env:NAME}}, never a value."
        else:
            message = f"{where or '/'}: {first['msg']}."
        raise self.schema_error(message, where)
