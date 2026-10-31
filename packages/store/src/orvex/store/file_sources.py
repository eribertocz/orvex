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
from collections.abc import Callable
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
_OFFSET_TIMESTAMP = (
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-](0\d|1[0-4])(:?[0-5]\d)?)"
)
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
    def reader_of(extra: str) -> str:
        return f"read_parquet([{listed}], union_by_name = true{extra})"

    widest, kinds = _parquet_schema(connection, listed)
    in_files = {key: name for key, (name, _) in kinds.items()}
    messages, forced, shared = _folders(connection, source, reader_of, files, in_files)
    reader = reader_of(forced)
    united = _columns(connection, reader)
    union = {fold(name): kind for name, kind in united}
    for key, (name, digits) in widest.items():
        messages.setdefault(
            key,
            f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} holds a DECIMAL with "
            f"{digits} digits, which cannot be read exactly.",
        )
    for key, (name, types) in kinds.items():
        target = union.get(key, "")
        lossy = sorted(kind for kind in types if not _exactly_in(kind, target))
        # A column shared with a folder serves the folder's value, proven equal by `_folders`.
        if key not in messages and key not in shared and target and lossy:
            messages[key] = (
                f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} has the types "
                f"{', '.join(sorted(types))} in different files; read together as {target}, "
                f"values of {', '.join(lossy)} would change."
            )
    parts = [
        f"error({literal(messages[fold(name)])}) AS {identifier(name)}"
        if fold(name) in messages
        else identifier(name)
        for name, _ in united
    ]
    # Every read first checks that no file's schema changed since the source was attached; the
    # check is one metadata scan, and filters still reach the reader (file pruning included).
    schema = (
        "(SELECT md5(CAST(list(s ORDER BY s.file_name, s.column_id) AS VARCHAR)) "
        f"FROM parquet_schema([{listed}]) AS s)"
    )
    attached = _row(connection, f"SELECT {schema}")
    changed = (
        f"UNSUPPORTED_SOURCE_TYPE: a file of source {source} changed its schema after the source "
        "was attached; attach the source again."
    )
    guard = (
        f"CASE WHEN {schema} = {literal(str(attached[0]) if attached else '')} THEN true "
        f"ELSE error({literal(changed)}) END"
    )
    return f"SELECT {', '.join(parts)} FROM {reader} WHERE {guard}"


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
    # Detection reads every row of every file once, here (`sample_size = -1`): with a sample, a
    # value after it could not change the detected type. It never yields floating point. The
    # view reads text with DuckDB's default sniffing, so a query does not scan the files twice;
    # every check below runs on that same reader.
    detected = _columns(
        connection,
        f"read_csv([{listed}], union_by_name = true, sample_size = -1, "
        f"auto_type_candidates = {CSV_TYPE_CANDIDATES})",
    )

    def reader_of(extra: str) -> str:
        return f"read_csv([{listed}], union_by_name = true, all_varchar = true{extra})"

    plain = _columns(connection, reader_of(", hive_partitioning = false"))
    in_files = {fold(name): name for name, _ in plain}
    messages, forced, _ = _folders(connection, source, reader_of, files, in_files)
    reader = reader_of(forced)
    columns = _columns(connection, reader)
    if [name for name, _ in detected] != [name for name, _ in columns]:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source} is read with different columns when its types are detected: "
            "check its header row.",
            {"source": source},
        )
    candidates = [
        (name, _CSV_TYPES[target])
        for (name, kind), (_, target) in zip(columns, detected, strict=True)
        if kind == "VARCHAR"
        and target in _CSV_TYPES
        and fold(name) in in_files
        and fold(name) not in messages
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
    for name, _ in columns:
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
        else:
            parts.append(column)  # exact text, or a folder value proven by `_folders`
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


def _folders(
    connection: duckdb.DuckDBPyConnection,
    source: str,
    reader_of: Callable[[str], str],
    files: list[str],
    in_files: dict[str, str],
) -> tuple[dict[str, str], str, set[str]]:
    """The columns DuckDB makes from `name=value` folders (Hive partitioning), checked against
    the values DuckDB itself serves: it decodes `%XX` in a value and may type it (`2026` as
    BIGINT). `reader_of(extra)` is the source's reader with `extra` options.

    Returns: messages for file columns that are also folder names and hold, in some file, other
    values than their folder's (DuckDB would serve the folder's); the `hive_types` option that
    keeps as text every folder value whose DuckDB type does not give its text back; and the
    folded names of the file columns shared with a folder, kept because their values agree.
    """
    keys = {fold(key) for file in files for key in _folder_names(file)}
    served = [name for name, _ in _columns(connection, reader_of(""))]
    folder = [name for name in served if fold(name) in keys]
    if not folder:
        return {}, "", set()
    marker = "source_file"
    while fold(marker) in {fold(name) for name in served}:
        marker = "_" + marker
    tag = f", filename = {literal(marker)}"
    by_file = identifier(marker)
    # Per file: the text DuckDB serves for each folder column (one value per file, or the
    # column is not a folder value there), and the same value as DuckDB types it.
    texts = ", ".join(
        f"max({identifier(name)}) AS t{index}, min({identifier(name)}) AS l{index}"
        for index, name in enumerate(folder)
    )
    typed = ", ".join(
        f"max(CAST({identifier(name)} AS VARCHAR)) AS d{index}" for index, name in enumerate(folder)
    )
    text_rows = (
        f"(SELECT {by_file}, {texts} FROM "
        f"{reader_of(', hive_types_autocast = false' + tag)} GROUP BY {by_file})"
    )
    typed_rows = f"(SELECT {by_file}, {typed} FROM {reader_of(tag)} GROUP BY {by_file})"
    keeps = ", ".join(
        f"coalesce(bool_and(t.t{index} = t.l{index} AND t.t{index} = d.d{index}), false)"
        for index in range(len(folder))
    )
    shared = [(index, name) for index, name in enumerate(folder) if fold(name) in in_files]
    agree = ", ".join(
        f"coalesce(bool_and(t.t{index} = t.l{index} AND CAST(f.{identifier(in_files[fold(name)])} "
        f"AS VARCHAR) IS NOT DISTINCT FROM t.t{index}), true)"
        for index, name in shared
    )
    row = _row(
        connection,
        f"SELECT [{keeps}] FROM {text_rows} AS t JOIN {typed_rows} AS d USING ({by_file})",
    )
    kept = row[0] if row and isinstance(row[0], list) else []
    agreed: list[object] = []
    if shared:  # every row of every file, against its folder's value
        files_rows = reader_of(", hive_partitioning = false" + tag)
        found = _row(
            connection,
            f"SELECT [{agree}] FROM {files_rows} AS f JOIN {text_rows} AS t USING ({by_file})",
        )
        agreed = found[0] if found and isinstance(found[0], list) else []
    messages = {}
    for position, (_, name) in enumerate(shared):
        if not (position < len(agreed) and agreed[position] is True):
            messages[fold(name)] = (
                f"UNSUPPORTED_SOURCE_TYPE: column {in_files[fold(name)]} of source {source} is "
                f"also the folder name {name}=…, and some file holds other values than its "
                "folder's: DuckDB would serve the folder's."
            )
    texts_only = [
        name for index, name in enumerate(folder) if not (index < len(kept) and kept[index] is True)
    ]
    forced = ""
    if texts_only:
        listed = ", ".join(f"{literal(name)}: 'VARCHAR'" for name in texts_only)
        forced = f", hive_types = {{{listed}}}"
    agreeing = {fold(name) for _, name in shared} - set(messages)
    return messages, forced, agreeing


def _folder_names(file: str) -> list[str]:
    """The names of the `name=value` folders of `file`, as DuckDB names their columns (it
    decodes the values, not the names)."""
    names = []
    for part in Path(file).parent.parts:
        name, equals, _ = part.partition("=")
        if equals and name:
            names.append(name)
    return names


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
