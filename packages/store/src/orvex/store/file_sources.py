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

# The types a CSV text column may take, in order: the first that gives back every value wins.
_CSV_ORDER = ("BOOLEAN", "BIGINT", "DATE", "TIMESTAMP", "TIMESTAMPTZ")
_CSV_SAMPLE = 20480  # rows on which each type is tried before one is checked on every row
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

    widest, kinds, duplicated, int96 = _parquet_schema(connection, listed)
    in_files = {key: name for key, (name, _) in kinds.items()}
    messages, forced, shared, folder_guard = _folders(
        connection, source, reader_of, files, in_files, parquet=listed
    )
    reader = reader_of(forced)
    united = _columns(connection, reader)
    union = {fold(name): kind for name, kind in united}
    if duplicated:
        # DuckDB renames such columns (x_1, a_1, C0), and a made-up name can be a real column
        # of another file: no check could tell which values a name stands for.
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source} has a file with two columns of the same name (ignoring case) or "
            "a column without a name; rename them in the file.",
            {"source": source},
        )
    for key, name in int96.items():
        messages.setdefault(
            key,
            f"UNSUPPORTED_SOURCE_TYPE: column {name} of source {source} holds an INT96 "
            "timestamp, which DuckDB reads only to the microsecond.",
        )
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
    return f"SELECT {', '.join(parts)} FROM {reader} WHERE {guard}{folder_guard}"


def _parquet_schema(
    connection: duckdb.DuckDBPyConnection, listed: str
) -> tuple[dict[str, tuple[str, int]], dict[str, tuple[str, set[str]]], set[str], dict[str, str]]:
    """Per top-level column (by folded name): the widest DECIMAL wider than 38 digits at any
    depth (struct field, list element, map key or value), and its type in each file; the
    folded names two top-level columns of one file share (or that are empty); and the columns
    holding an INT96 timestamp at any depth.

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
        "OR logical_type LIKE 'DecimalType(%') IS TRUE AS VARCHAR), coalesce(duckdb_type, ''), "
        "coalesce(type, '')] "
        f"ORDER BY column_id) AS nodes FROM parquet_schema([{listed}]) GROUP BY file_name)",
    )
    widest: dict[str, tuple[str, int]] = {}
    kinds: dict[str, tuple[str, set[str]]] = {}
    duplicated: set[str] = set()
    int96: dict[str, str] = {}
    for nodes in row[0] if row and isinstance(row[0], list) else []:
        remaining = [int(nodes[0][1])]  # children still to visit, per open group (root first)
        top = ""
        in_file: set[str] = set()
        for name, children, digits, is_decimal, kind, physical in nodes[1:]:
            if len(remaining) == 1:
                top = str(name)  # a child of the root: a top-level column
                if fold(top) in in_file or not top:
                    duplicated.add(fold(top))
                in_file.add(fold(top))
                kinds.setdefault(fold(top), (top, set()))[1].add(str(kind))
            if is_decimal == "true" and int(digits) > widest.get(fold(top), ("", 38))[1]:
                widest[fold(top)] = (top, int(digits))
            if physical == "INT96":
                int96[fold(top)] = top
            remaining[-1] -= 1
            if int(children) > 0:
                remaining.append(int(children))
            while remaining and remaining[-1] == 0:
                remaining.pop()
    return widest, kinds, duplicated, int96


def _csv_view(
    connection: duckdb.DuckDBPyConnection, source: str, listed: str, files: list[str]
) -> str:
    # One reader, with DuckDB's own sniffing (dialect, header): the view reads with it, and every
    # check below runs on it, so what is checked is what is served. Types are not sniffed: each
    # text column takes the first type of `_CSV_ORDER` that gives back every one of its values,
    # checked over every row of every file in one scan; it never takes floating point.
    def reader_of(extra: str) -> str:
        return f"read_csv([{listed}], union_by_name = true, all_varchar = true{extra})"

    plain = _columns(connection, reader_of(", hive_partitioning = false"))
    in_files = {fold(name): name for name, _ in plain}
    messages, forced, _, folder_guard = _folders(connection, source, reader_of, files, in_files)
    reader = reader_of(forced)
    columns = _columns(connection, reader)
    candidates = [
        (name, target)
        for name, kind in columns
        if kind == "VARCHAR" and fold(name) in in_files and fold(name) not in messages
        for target in _CSV_ORDER
    ]
    proven: dict[str, str] = {}
    if candidates:
        # Each type is first tried on the first rows (cheap), and only the one that fits there
        # is checked over every row of every file (one condition per column, one scan). A
        # column with no value at all keeps its text: no value proves a type.
        tries = ", ".join(
            f"count({identifier(name)}) > 0 AND coalesce(bool_and({_keeps(name, target)}), false)"
            for name, target in candidates
        )
        sample = _row(connection, f"SELECT [{tries}] FROM (FROM {reader} LIMIT {_CSV_SAMPLE})")
        fits = sample[0] if sample and isinstance(sample[0], list) else []
        proposed: dict[str, str] = {}
        for (name, target), ok in zip(candidates, fits, strict=False):
            if ok is True and name not in proposed:
                proposed[name] = target
        if proposed:
            checks = ", ".join(
                f"coalesce(bool_and({_keeps(name, target)}), false)"
                for name, target in proposed.items()
            )
            row = _row(connection, f"SELECT [{checks}] FROM {reader}")
            kept = row[0] if row and isinstance(row[0], list) else []
            for (name, target), ok in zip(proposed.items(), kept, strict=False):
                if ok is True:
                    proven[name] = target
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
    # Every read first checks that no file's header changed since the source was attached: a
    # column added later could take a folder's name and be served the folder's value.
    header = (
        "(SELECT md5(CAST(list(column_name) AS VARCHAR)) FROM "
        f"(DESCRIBE SELECT * FROM {reader_of(', hive_partitioning = false')}))"
    )
    attached = _row(connection, f"SELECT {header}")
    changed = (
        f"UNSUPPORTED_SOURCE_TYPE: a file of source {source} changed its header after the "
        "source was attached; attach the source again."
    )
    guard = (
        f"CASE WHEN {header} = {literal(str(attached[0]) if attached else '')} THEN true "
        f"ELSE error({literal(changed)}) END"
    )
    return f"SELECT {', '.join(parts)} FROM {reader} WHERE {guard}{folder_guard}"


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
    parquet: str = "",
) -> tuple[dict[str, str], str, set[str], str]:
    """The columns DuckDB makes from `name=value` folders (Hive partitioning), checked against
    the values DuckDB itself serves: it decodes `%XX` in a value and may type it (`2026` as
    BIGINT). `reader_of(extra)` is the source's reader with `extra` options.

    Returns: messages for file columns that are also folder names and hold, in some file, other
    values than their folder's (DuckDB would serve the folder's); the `hive_types` option that
    keeps as text every folder value whose DuckDB type does not give its text back; the folded
    names of the file columns shared with a folder, kept because their values agree; and
    ` AND …`, the condition that checks those agreeing columns again on every read, so a file
    rewritten afterwards fails instead of being served its folder's value. Every comparison
    treats NULL as a value (`IS NOT DISTINCT FROM`), never as a pass.

    For Parquet (`parquet` lists its files) a shared column is compared through each file's
    footer statistics, never its rows: every row group's minimum and maximum are the folder's
    text and it holds no NULL. The same metadata check runs on every read; a file without such
    statistics fails it, so its column is refused (fail closed). CSV files have no statistics:
    their rows are compared, on every read too.
    """
    keys = {fold(key) for file in files for key in _folder_names(file)}
    served = [name for name, _ in _columns(connection, reader_of(""))]
    folder = [name for name in served if fold(name) in keys]
    if not folder:
        return {}, "", set(), ""
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
        f"coalesce(bool_and(t.t{index} IS NOT DISTINCT FROM t.l{index} "
        f"AND t.t{index} IS NOT DISTINCT FROM d.d{index}), false)"
        for index in range(len(folder))
    )
    shared = [(index, name) for index, name in enumerate(folder) if fold(name) in in_files]
    row = _row(
        connection,
        f"SELECT [{keeps}] FROM {text_rows} AS t JOIN {typed_rows} AS d USING ({by_file})",
    )
    kept = row[0] if row and isinstance(row[0], list) else []
    texts_only = [
        name for index, name in enumerate(folder) if not (index < len(kept) and kept[index] is True)
    ]
    forced = ""
    if texts_only:
        listed = ", ".join(f"{literal(name)}: 'VARCHAR'" for name in texts_only)
        forced = f", hive_types = {{{listed}}}"
    if not shared:
        return {}, forced, set(), ""
    # One value per file, or the folder is not that file's value: no text then (never equal).
    columns = ", ".join(
        f"CASE WHEN t{index} IS NOT DISTINCT FROM l{index} THEN t{index} END" for index, _ in shared
    )
    single = ", ".join(f"t{index} IS NOT DISTINCT FROM l{index}" for index, _ in shared)
    values = _row(
        connection,
        f"SELECT list({{'file': {by_file}, 'texts': [{columns}], 'single': [{single}]}}) "
        f"FROM {text_rows}",
    )
    per_file = values[0] if values and isinstance(values[0], list) else []
    checks = []
    for position, (_, name) in enumerate(shared):
        column = in_files[fold(name)]
        folder_text = {
            str(entry["file"]): entry["texts"][position] if entry["single"][position] else None
            for entry in per_file
        }
        if parquet:
            cases = " ".join(
                f"WHEN {literal(file)} THEN "
                + (
                    "false"
                    if text is None
                    else f"stats_min_value IS NOT DISTINCT FROM {literal(str(text))} "
                    f"AND stats_max_value IS NOT DISTINCT FROM {literal(str(text))} "
                    "AND stats_null_count = 0"
                )
                for file, text in folder_text.items()
            )
            # `lower` may match more column chunks than DuckDB's ASCII-only rule, never fewer: an
            # extra one is checked too (fail closed).
            checks.append(
                f"(SELECT count(*) > 0 AND coalesce(bool_and(CASE file_name {cases} ELSE false "
                f"END), false) FROM parquet_metadata([{parquet}]) "
                f"WHERE lower(path_in_schema) = lower({literal(column)}))"
            )
        else:
            value = f"CAST({identifier(column)} AS VARCHAR)"
            cases = " ".join(
                f"WHEN {literal(file)} THEN {value} IS NOT DISTINCT FROM "
                + ("NULL" if text is None else literal(str(text)))
                for file, text in folder_text.items()
            )
            files_rows = reader_of(", hive_partitioning = false" + tag)
            checks.append(
                f"(SELECT coalesce(bool_and(CASE {by_file} {cases} ELSE false END), true) "
                f"FROM {files_rows})"
            )
    # The check that decides now is the one every read runs.
    found = _row(connection, f"SELECT [{', '.join(checks)}]")
    agreed = found[0] if found and isinstance(found[0], list) else []
    messages = {}
    kept_checks = []
    for position, (_, name) in enumerate(shared):
        if position < len(agreed) and agreed[position] is True:
            kept_checks.append(checks[position])
            continue
        messages[fold(name)] = (
            f"UNSUPPORTED_SOURCE_TYPE: column {in_files[fold(name)]} of source {source} is "
            f"also the folder name {name}=…, and some file holds other values than its folder's "
            "(or, for Parquet, no statistics that prove it): DuckDB would serve the folder's."
        )
    guard = ""
    if kept_checks:
        message = (
            f"UNSUPPORTED_SOURCE_TYPE: a file of source {source} now holds, in a column shared "
            "with its folder name, another value than its folder's; attach the source again."
        )
        guard = (
            f" AND CASE WHEN {' AND '.join(kept_checks)} THEN true "
            f"ELSE error({literal(message)}) END"
        )
    agreeing = {fold(name) for _, name in shared} - set(messages)
    return messages, forced, agreeing, guard


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
    if kind in (target, "NULL", '"NULL"') or target == "VARCHAR":
        return True  # a column of only NULLs (Parquet's null type) holds no value to change
    if kind == "FLOAT" and target == "DOUBLE":
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
