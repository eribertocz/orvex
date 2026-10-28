"""Find and check a project's models (P1-08, contract `model-config` v1).

A model is `models/<folder>/<name>.sql` and builds the table `<folder>.<name>`; an optional
`<name>.yaml` beside it configures it. Discovery reads the folder and nothing else: it never
writes, never runs a model, and reports every problem it finds at once.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
from orvex.contracts.model_config.v1 import ModelConfig, ModelError, ModelErrorDetails
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.transform.config_file import ConfigFile, ConfigProblem
from orvex.transform.sources import Connection

MODELS_FOLDER = "models"
SUPPORTED_VERSION = 1
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
RESERVED_SCHEMAS = frozenset({"lake"})
MAX_SQL_BYTES = 1024 * 1024
_PARSER_LINE = re.compile(r"LINE (\d+):")


@dataclass(frozen=True, slots=True)
class Model:
    """One model: `schema.table`, its SQL as written, and its configuration (defaults applied).

    `extraction` models name a ClickHouse connection; their SQL is that source's dialect.
    """

    schema: str
    table: str
    sql_file: str  # relative to the project folder
    sql: str
    config: ModelConfig
    config_file: str | None
    # The sidecar's numbers as written, by JSON pointer (exact text for bounds and tolerances).
    numbers: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def extraction(self) -> bool:
        return self.config.connection is not None


@dataclass(frozen=True, slots=True)
class Discovery:
    models: tuple[Model, ...]
    problems: tuple[ModelError, ...]


def problem(
    code: str,
    message: str,
    file: str,
    *,
    model: str | None = None,
    path: str | None = None,
    line: int | None = None,
) -> ModelError:
    return ModelError(
        code=code,  # type: ignore[arg-type]  # validated against the contract's codes
        message=message,
        details=ModelErrorDetails(file=file, model=model, path=path, line=line),
    )


def discover_models(folder: Path, connections: Mapping[str, Connection]) -> Discovery:
    """Every model of the project in `folder`, sorted by name, and every problem found."""
    root = Path(folder) / MODELS_FOLDER
    problems: list[ModelError] = []
    models: list[Model] = []
    if not root.is_dir():
        return Discovery((), ())
    files: list[Path] = []

    def unreadable(error: OSError) -> None:
        where = Path(error.filename or root).relative_to(folder).as_posix()
        problems.append(
            problem("MODEL_PATH_INVALID", f"{where} cannot be read ({error.strerror}).", where)
        )

    for directory, folders, names in os.walk(root, onerror=unreadable):
        here = Path(directory)
        visible = sorted(name for name in folders if not name.startswith("."))
        # Links are not followed: a model is a file of the project, never one elsewhere.
        links = [name for name in visible if (here / name).is_symlink()]
        folders[:] = [name for name in visible if name not in links]
        for name in links + sorted(n for n in names if not n.startswith(".")):
            path = here / name
            if path.is_symlink():
                where = path.relative_to(folder).as_posix()
                problems.append(
                    problem(
                        "MODEL_PATH_INVALID",
                        f"{where} is a symbolic link; models must be files of the project.",
                        where,
                    )
                )
            else:
                files.append(path)
    sql_files = {path for path in files if path.suffix == ".sql"}
    conflicts_reported: set[str] = set()
    for path in files:
        relative = path.relative_to(folder).as_posix()
        parts = path.relative_to(root).parts
        suffix = path.suffix
        if suffix.lower() in {".sql", ".yaml", ".yml", ".py"} and suffix != suffix.lower():
            problems.append(
                problem(
                    "MODEL_PATH_INVALID",
                    f"{relative}: model extensions are lowercase ({suffix.lower()}).",
                    relative,
                )
            )
            continue
        if suffix not in {".sql", ".yaml", ".yml", ".py"}:
            continue  # README.md and other files are not models
        if len(parts) != 2:
            problems.append(
                problem(
                    "MODEL_PATH_INVALID",
                    f"{relative}: models live exactly one folder below models/ "
                    "(models/<folder>/<name>.sql).",
                    relative,
                )
            )
            continue
        schema, stem = parts[0], path.stem
        if not NAME.fullmatch(schema) or not NAME.fullmatch(stem):
            problems.append(
                problem(
                    "MODEL_PATH_INVALID",
                    f"{relative}: folder and file names must be lowercase identifiers "
                    "(a letter, then letters, digits or _).",
                    relative,
                )
            )
            continue
        if suffix == ".yml":
            problems.append(
                problem(
                    "MODEL_PATH_INVALID",
                    f"{relative}: sidecars use the .yaml extension.",
                    relative,
                    model=f"{schema}.{stem}",
                )
            )
            continue
        if schema in connections or schema in RESERVED_SCHEMAS:
            if schema not in conflicts_reported:
                conflicts_reported.add(schema)
                what = "a connection" if schema in connections else "reserved"
                problems.append(
                    problem(
                        "MODEL_SCHEMA_CONFLICT",
                        f"models/{schema}/: the folder name is {what}; rename the folder.",
                        f"models/{schema}",
                    )
                )
            continue
        if suffix == ".py":
            problems.append(
                problem(
                    "UNSUPPORTED_MODEL_KIND",
                    f"{relative}: Python models are not supported in this version (ADR-0023).",
                    relative,
                    model=f"{schema}.{stem}",
                )
            )
            continue
        if suffix == ".yaml":
            if path.with_suffix(".sql") not in sql_files:
                problems.append(
                    problem(
                        "ORPHAN_MODEL_CONFIG",
                        f"{relative} has no {stem}.sql beside it.",
                        relative,
                        model=f"{schema}.{stem}",
                    )
                )
            continue
        found = _model(folder, path, schema, stem, connections)
        if isinstance(found, Model):
            models.append(found)
        else:
            problems += found
    models.sort(key=lambda model: model.name)
    problems.sort(key=lambda item: (item.details.file, item.code.root))
    return Discovery(tuple(models), tuple(problems))


def _model(
    folder: Path, path: Path, schema: str, stem: str, connections: Mapping[str, Connection]
) -> Model | list[ModelError]:
    relative = path.relative_to(folder).as_posix()
    name = f"{schema}.{stem}"
    problems: list[ModelError] = []
    config = ModelConfig(version=1)
    numbers: dict[str, str] = {}
    sidecar = path.with_suffix(".yaml")
    config_file: str | None = None
    if sidecar.exists() or sidecar.is_symlink():
        config_file = sidecar.relative_to(folder).as_posix()
        loaded = _config(sidecar, config_file, name, connections)
        if isinstance(loaded, ModelError):
            problems.append(loaded)
        else:
            config, numbers = loaded
    sql = _read_sql(path, relative, name)
    if isinstance(sql, ModelError):
        return [*problems, sql]
    if config.connection is None:
        invalid = _check_select(sql, relative, name)
        if invalid is not None:
            problems.append(invalid)
    if problems:
        return problems
    return Model(schema, stem, relative, sql, config, config_file, numbers)


def _config(
    path: Path, relative: str, name: str, connections: Mapping[str, Connection]
) -> tuple[ModelConfig, dict[str, str]] | ModelError:
    issue: ConfigProblem | None = None
    try:
        document = ConfigFile(path, SUPPORTED_VERSION, name=relative)
        config = document.model(ModelConfig, document.data, "")
    except ConfigProblem as raised:
        issue = raised
    if issue is not None:
        # Outside the handler: the problem is reported without the original as context.
        return problem(
            "MODEL_CONFIG_INVALID",
            issue.message,
            relative,
            model=name,
            path=issue.path,
            line=issue.line,
        )
    repeated = _repeated_item(document.data)
    if repeated is not None:
        return problem(
            "MODEL_CONFIG_INVALID",
            f"{relative}: {repeated}: a column is listed more than once.",
            relative,
            model=name,
            path=repeated,
            line=document.line(repeated),
        )
    if config.connection is not None:
        connection = connections.get(config.connection)
        if not isinstance(connection, ClickhouseConnection):
            what = "not a connection of the project" if connection is None else "not ClickHouse"
            return problem(
                "MODEL_CONFIG_INVALID",
                f"{relative}: connection '{config.connection}' is {what}; extraction models "
                "read from a ClickHouse connection.",
                relative,
                model=name,
                path="/connection",
                line=document.line("/connection"),
            )
    return config, document.numbers()


def _repeated_item(data: object) -> str | None:
    """JSON pointer of the first column list with a repeated entry (the schema's uniqueItems)."""
    if not isinstance(data, dict):
        return None
    lists: list[tuple[str, object]] = []
    incremental = data.get("incremental")
    if isinstance(incremental, dict):
        lists += [
            (f"/incremental/{key}", incremental.get(key)) for key in ("unique_key", "partition_by")
        ]
    tests = data.get("tests")
    if isinstance(tests, list):
        for index, test in enumerate(tests):
            if isinstance(test, dict):
                lists += [
                    (f"/tests/{index}/{key}", test.get(key)) for key in ("unique", "not_null")
                ]
    for pointer, items in lists:
        if isinstance(items, list) and len(items) != len({str(item) for item in items}):
            return pointer
    return None


def _read_sql(path: Path, relative: str, name: str) -> str | ModelError:
    failure: ModelError | None = None
    text = ""
    data = b""
    try:
        # Opened without blocking and checked on the descriptor: a pipe named .sql must not
        # hang discovery.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                data = handle.read(MAX_SQL_BYTES + 1)
            else:
                failure = problem(
                    "INVALID_MODEL_SQL", f"{relative} is not a regular file.", relative, model=name
                )
        if failure is not None:
            pass
        elif len(data) > MAX_SQL_BYTES:
            failure = problem(
                "INVALID_MODEL_SQL", f"{relative}: larger than 1024 KiB.", relative, model=name
            )
        else:
            text = data.decode("utf-8")
    except OSError as error:
        failure = problem(
            "INVALID_MODEL_SQL",
            f"{relative} cannot be read ({error.strerror or type(error).__name__}).",
            relative,
            model=name,
        )
    except UnicodeDecodeError as error:
        failure = problem(
            "INVALID_MODEL_SQL",
            f"{relative}: not UTF-8 text (byte {error.start}).",
            relative,
            model=name,
        )
    return failure if failure is not None else text


def _check_select(sql: str, relative: str, name: str) -> ModelError | None:
    """None when `sql` is exactly one SELECT for DuckDB's parser (no binding, no data)."""
    message: str | None = None
    line: int | None = None
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        text = str(error)
        found = _PARSER_LINE.search(text)
        line = int(found.group(1)) if found else None
        message = f"{relative}: {text.splitlines()[0] if text else 'the SQL cannot be parsed'}"
        statements = []
    if message is None and len(statements) != 1:
        message = f"{relative}: a model is exactly one SELECT statement; found {len(statements)}."
    elif message is None and statements[0].type != duckdb.StatementType.SELECT:
        message = (
            f"{relative}: a model is a SELECT (or WITH … SELECT); found {statements[0].type.name}."
        )
    if message is None:
        return None
    return problem("INVALID_MODEL_SQL", message, relative, model=name, line=line)
