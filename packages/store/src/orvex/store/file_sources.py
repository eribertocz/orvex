"""Exact views over file sources: CSV and Parquet files, read by DuckDB only (#252, law 11).

A file source is served through one view. Every column of it either carries the files' values
exactly or fails when read, with a message naming `UNSUPPORTED_SOURCE_TYPE`, the source and the
column; queries that do not read such a column keep working. Each column is checked once when
the source is attached, and its view expression checks it again on every read, so a file
rewritten later fails instead of changing a value:

- **Parquet.** A column holding, at any depth, a DECIMAL wider than 38 digits is refused (DuckDB
  reads it as DOUBLE); so is a column whose types in different files unite (`union_by_name`)
  into a type that cannot hold every file's values. Every other column is pinned to the type it
  had when attached.
- **CSV.** Every value is read as its text. A column keeps a detected type (BOOLEAN, BIGINT,
  DATE, TIMESTAMP, or TIMESTAMPTZ with an explicit offset) only when that type gives back every
  value's text; otherwise it stays exact text, which the model casts explicitly.
- **Folders.** A file column that is also a folder name (`year=2026`, Hive partitioning) is
  refused when some file holds a value other than its folder's: DuckDB would serve the folder's.

Column names match the way DuckDB matches them: ignoring the case of ASCII letters only.
Only metadata and one aggregate row per check reach Python (ADR-0025).
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import duckdb
from orvex.store.errors import StoreError
from orvex.store.sql_text import identifier, literal

CSV_TYPE_CANDIDATES = "['BOOLEAN', 'BIGINT', 'DATE', 'TIMESTAMP', 'TIMESTAMPTZ', 'VARCHAR']"
# Detected type, as DESCRIBE names it -> the type a column keeps when every text survives it.
_CSV_TYPES = {
    "BOOLEAN": "BOOLEAN",
    "BIGINT": "BIGINT",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}
# A timestamp DuckDB reads to the microsecond with an explicit offset: its instant is exact.
_OFFSET_TIMESTAMP = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}(:?\d{2})?)"
_DECIMAL_TYPE = re.compile(r"DECIMAL\((\d+),(\d+)\)", re.ASCII)
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "INTEGER": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "HUGEINT": (-(2**127), 2**127 - 1),
    "UTINYINT": (0, 2**8 - 1),
    "USMALLINT": (0, 2**16 - 1),
    "UINTEGER": (0, 2**32 - 1),
    "UBIGINT": (0, 2**64 - 1),
    "UHUGEINT": (0, 2**128 - 1),
}


def view_query(
    connection: duckdb.DuckDBPyConnection, kind: str, source: str, files: list[str]
) -> str:
    """The `SELECT` of the view serving file source `source` of type `kind` (`parquet` or
    `csv`) over exactly `files`."""
    listed = ", ".join(literal(file) for file in files)
    if kind == "parquet":
        return _parquet_view(connection, source, listed, files)
    return _csv_view(connection, source, listed, files)


def fold(name: str) -> str:
    """`name` as DuckDB compares column names: ASCII letters in lower case, nothing else."""
    return name.translate(_ASCII_LOWER)


def _parquet_view(
    connection: duckdb.DuckDBPyConnection, source: str, listed: str, files: list[str]
) -> str:
    reader = f"read_parquet([{listed}], union_by_name = true)"
    united = _columns(connection, reader)
    union = {fold(name): kind for name, kind in united}
    widest, kinds = _parquet_schema(connection, listed)
    messages = {
        key: f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} holds a DECIMAL with "
        f"{digits} digits, which cannot be read exactly."
        for key, (name, digits) in widest.items()
    }
    for key, (name, types) in kinds.items():
        target = union.get(key, "")
        lossy = sorted(kind for kind in types if not _exactly_in(kind, target))
        if key not in messages and target and lossy:
            messages[key] = (
                f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} has the types "
                f"{', '.join(sorted(types))} in different files; read together as {target}, "
                f"values of {', '.join(lossy)} would change."
            )
    in_files = {key: name for key, (name, _) in kinds.items()}
    opening = f"read_parquet([{listed}], union_by_name = true, hive_partitioning = false"
    messages.update(_folder_conflicts(connection, source, opening, files, in_files, messages))
    parts = [
        f"error({literal(messages[fold(name)])}) AS {identifier(name)}"
        if fold(name) in messages
        else _pinned(name, kind, source)
        for name, kind in united
    ]
    return f"SELECT {', '.join(parts)} FROM {reader}"


def _parquet_schema(
    connection: duckdb.DuckDBPyConnection, listed: str
) -> tuple[dict[str, tuple[str, int]], dict[str, tuple[str, set[str]]]]:
    """Per top-level column (by folded name): the widest DECIMAL wider than 38 digits at any
    depth (struct field, list element, map key or value), and its type in each file.

    From DuckDB's `parquet_schema`, the schema its reader uses (decimal annotations, converted
    or logical: the precision of a logical-only one is read from `logical_type`, as `precision`
    is empty then; never Arrow metadata stored in the file). Nodes come in schema order
    (`column_id`), each group followed by its `num_children` children, so every leaf maps to
    its top-level column.
    """
    row = _row(
        connection,
        "SELECT list(nodes) FROM (SELECT list([name, CAST(coalesce(num_children, 0) AS VARCHAR), "
        "CAST(coalesce(precision, TRY_CAST(regexp_extract(logical_type, 'precision=(\\d+)', 1) "
        "AS INTEGER), 0) AS VARCHAR), CAST((converted_type = 'DECIMAL' "
        "OR logical_type LIKE 'DecimalType(%') IS TRUE AS VARCHAR), coalesce(duckdb_type, '')] "
        f"ORDER BY column_id) AS nodes FROM parquet_schema([{listed}]) GROUP BY file_name)",
    )
    widest: dict[str, tuple[str, int]] = {}
    kinds: dict[str, tuple[str, set[str]]] = {}
    for nodes in row[0] if row and isinstance(row[0], list) else []:
        remaining = [int(nodes[0][1])]  # children still to visit, per open group (root first)
        top = ""
        for name, children, digits, is_decimal, kind in nodes[1:]:
            if len(remaining) == 1:
                top = str(name)  # a child of the root: a top-level column
                kinds.setdefault(fold(top), (top, set()))[1].add(str(kind))
            if is_decimal == "true" and int(digits) > widest.get(fold(top), ("", 38))[1]:
                widest[fold(top)] = (top, int(digits))
            remaining[-1] -= 1
            if int(children) > 0:
                remaining.append(int(children))
            while remaining and remaining[-1] == 0:
                remaining.pop()
    return widest, kinds


def _csv_view(
    connection: duckdb.DuckDBPyConnection, source: str, listed: str, files: list[str]
) -> str:
    # The whole file is read for detection (`sample_size = -1`): with a sample, a value after
    # it could not change the detected type. Detection never yields floating point.
    options = f"[{listed}], union_by_name = true, sample_size = -1"
    detected = _columns(
        connection, f"read_csv({options}, auto_type_candidates = {CSV_TYPE_CANDIDATES})"
    )
    reader = f"read_csv({options}, all_varchar = true)"
    columns = _columns(connection, reader)
    if [name for name, _ in detected] != [name for name, _ in columns]:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source} is read with different columns when its types are detected: "
            "check its header row.",
            {"source": source},
        )
    plain = _columns(
        connection, f"read_csv({options}, all_varchar = true, hive_partitioning = false)"
    )
    in_files = {fold(name): name for name, _ in plain}
    opening = f"read_csv({options}, all_varchar = true, hive_partitioning = false"
    messages = _folder_conflicts(connection, source, opening, files, in_files, {})
    candidates = [
        (name, _CSV_TYPES[target])
        for (name, kind), (_, target) in zip(columns, detected, strict=True)
        if kind == "VARCHAR" and target in _CSV_TYPES and fold(name) not in messages
    ]
    proven: dict[str, str] = {}
    if candidates:
        checks = ", ".join(
            f"coalesce(bool_and({_keeps(name, target)}), false)" for name, target in candidates
        )
        row = _row(connection, f"SELECT [{checks}] FROM {reader}")
        kept = row[0] if row and isinstance(row[0], list) else [False] * len(candidates)
        pairs = zip(candidates, kept, strict=True)
        proven = {name: target for (name, target), ok in pairs if ok is True}
    parts = []
    for name, kind in columns:
        column = identifier(name)
        if fold(name) in messages:
            parts.append(f"error({literal(messages[fold(name)])}) AS {column}")
        elif name in proven:
            target = proven[name]
            message = (
                f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} now holds a value "
                f"that {target} would not keep exactly; attach the source again."
            )
            parts.append(
                f"CASE WHEN {_keeps(name, target)} THEN TRY_CAST({column} AS {target}) "
                f"ELSE error({literal(message)}) END AS {column}"
            )
        elif kind == "VARCHAR":
            parts.append(column)
        else:
            parts.append(_pinned(name, kind, source))  # a folder value DuckDB typed itself
    return f"SELECT {', '.join(parts)} FROM {reader}"


def _keeps(name: str, target: str) -> str:
    """A condition true when the text in column `name` converts to `target` without change."""
    column = identifier(name)
    if target == "TIMESTAMPTZ":
        return (
            f"({column} IS NULL OR (regexp_full_match({column}, {literal(_OFFSET_TIMESTAMP)}) "
            f"AND TRY_CAST({column} AS TIMESTAMPTZ) IS NOT NULL))"
        )
    text = f"lower({column})" if target == "BOOLEAN" else column
    # A text the type cannot read gives NULL, which must count as a change, not be skipped.
    converted = f"CAST(TRY_CAST({column} AS {target}) AS VARCHAR)"
    return f"({column} IS NULL OR coalesce({converted} = {text}, false))"


def _pinned(name: str, kind: str, source: str) -> str:
    """Column `name` as long as DuckDB still reads it as `kind`; a file rewritten after the
    source was attached fails instead of serving another type. DuckDB folds the test away when
    planning, so filters still reach the reader."""
    column = identifier(name)
    message = (
        f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} was {kind} when the source "
        "was attached and its files now give another type; attach the source again."
    )
    return (
        f"CASE WHEN typeof({column}) = {literal(kind)} THEN {column} "
        f"ELSE error({literal(message)}) END AS {column}"
    )


def _folder_conflicts(
    connection: duckdb.DuckDBPyConnection,
    source: str,
    opening: str,
    files: list[str],
    in_files: dict[str, str],
    refused: dict[str, str],
) -> dict[str, str]:
    """Messages for the file columns that are also folder names (`name=value` in a file's
    directories) and hold, in some file, another value than that folder's. `opening` is the
    reader without folder columns, missing its closing parenthesis."""
    folders = [_folder_values(file) for file in files]
    keys = sorted(key for key in {key for values in folders for key in values} if key in in_files)
    keys = [key for key in keys if key not in refused]
    if not keys:
        return {}
    marker = "source_file"
    while fold(marker) in in_files:
        marker = "_" + marker
    checks = []
    for key in keys:
        column = f"CAST({identifier(in_files[key])} AS VARCHAR)"
        cases = " ".join(
            f"WHEN {literal(file)} THEN "
            + (
                f"{column} IS NOT DISTINCT FROM {literal(values[key][1])}"
                if key in values
                else "true"
            )
            for file, values in zip(files, folders, strict=True)
        )
        # A file DuckDB names otherwise is counted as a conflict (fail closed).
        checks.append(f"coalesce(bool_and(CASE {identifier(marker)} {cases} ELSE false END), true)")
    row = _row(
        connection, f"SELECT [{', '.join(checks)}] FROM {opening}, filename = {literal(marker)})"
    )
    agree = row[0] if row and isinstance(row[0], list) else []
    messages = {}
    for index, key in enumerate(keys):
        if index < len(agree) and agree[index] is True:
            continue
        folder = next(values[key][0] for values in folders if key in values)
        messages[key] = (
            f"UNSUPPORTED_SOURCE_TYPE: column {in_files[key]} of source {source} is also the "
            f"folder name {folder}=…, and some file holds other values than its folder's: "
            "DuckDB would serve the folder's."
        )
    return messages


def _folder_values(file: str) -> dict[str, tuple[str, str]]:
    """The `name=value` folders of `file`, by folded name: (name, value)."""
    values = {}
    for part in Path(file).parent.parts:
        name, equals, value = part.partition("=")
        if equals and name:
            values[fold(name)] = (name, value)
    return values


def _columns(connection: duckdb.DuckDBPyConnection, reader: str) -> list[tuple[str, str]]:
    """Name and DuckDB type of every column `reader` gives, in order."""
    row = _row(
        connection,
        f"SELECT list([column_name, column_type]) FROM (DESCRIBE SELECT * FROM {reader})",
    )
    pairs = row[0] if row and isinstance(row[0], list) else []
    return [(str(name), str(kind)) for name, kind in pairs]


def _row(connection: duckdb.DuckDBPyConnection, sql: str) -> tuple[object, ...]:
    """The single row of a metadata or aggregate query."""
    result = connection.execute(sql)
    row = result.fetchone()
    result.fetchone()  # reaches the end of the result, so the connection is free
    return tuple(row) if row is not None else ()


def _exactly_in(kind: str, target: str) -> bool:
    """Whether every value of DuckDB type `kind` converts to `target` without change. Proven
    cases only (same type, wider integer, integer or decimal into a decimal with enough
    integer and fraction digits, DATE into TIMESTAMP, anything into VARCHAR text); every other
    pair counts as lossy (fail closed)."""
    if kind == target or target == "VARCHAR":
        return True
    if kind == "DATE" and target == "TIMESTAMP":
        return True
    decimal_target = _decimal(target)
    if kind in _INTEGER_RANGES:
        low, high = _INTEGER_RANGES[kind]
        if target in _INTEGER_RANGES:
            target_low, target_high = _INTEGER_RANGES[target]
            return target_low <= low and high <= target_high
        if decimal_target:
            precision, scale = decimal_target
            # 10**digits > bound exactly when digits covers every digit of the bound.
            return precision - scale >= len(str(max(-low, high)))
        return False
    decimal_kind = _decimal(kind)
    if decimal_kind and decimal_target:
        (precision, scale), (target_precision, target_scale) = decimal_kind, decimal_target
        return target_scale >= scale and target_precision - target_scale >= precision - scale
    return False


def _decimal(kind: str) -> tuple[int, int] | None:
    """Precision and scale of a `DECIMAL(p,s)` type name; None for any other type."""
    match = _DECIMAL_TYPE.fullmatch(kind)
    return (int(match[1]), int(match[2])) if match else None
