"""The statement guard of the managed lake's SQL: what a statement may name and call, the one
statement it must be, and the statement kinds a transaction may run (a first layer before the
connection's sandbox).
"""

from __future__ import annotations

import itertools

import duckdb
from orvex.store.errors import StoreError
from orvex.store.file_names import _refuse_file_names
from orvex.store.lake_names import METADATA_PREFIX
from orvex.store.sql_parser import _plain, _tokens

# The engine's own catalog, logs and settings, reachable by name from any SQL (#256, measured on
# the pinned DuckDB; a test fails when an upgrade adds one): the views of `pg_catalog` and the
# internal `duckdb_*`/`sqlite_*` views (attach strings, logged queries), the setting and query
# readers, and the `pg_catalog` functions. Never readable from model or read SQL. Two names stay
# allowed, and only these: `information_schema` (the names and types of the lake's own tables,
# which incremental models compare) and `current_database()` (the lake's name).
_ENGINE_NAMES = frozenset(
    {
        "col_description",
        "current_query",
        "current_schema",
        "current_schemas",
        "current_setting",
        "duckdb_columns",
        "duckdb_constraints",
        "duckdb_databases",
        "duckdb_indexes",
        "duckdb_logs",
        "duckdb_schemas",
        "duckdb_tables",
        "duckdb_types",
        "duckdb_views",
        "format_pg_type",
        "format_type",
        "getvariable",
        "has_any_column_privilege",
        "has_column_privilege",
        "has_database_privilege",
        "has_foreign_data_wrapper_privilege",
        "has_function_privilege",
        "has_language_privilege",
        "has_schema_privilege",
        "has_sequence_privilege",
        "has_server_privilege",
        "has_table_privilege",
        "has_tablespace_privilege",
        "inet_client_addr",
        "inet_client_port",
        "inet_server_addr",
        "inet_server_port",
        "map_to_pg_oid",
        "obj_description",
        "pg_am",
        "pg_attrdef",
        "pg_attribute",
        "pg_catalog",
        "pg_class",
        "pg_collation",
        "pg_collation_is_visible",
        "pg_conf_load_time",
        "pg_constraint",
        "pg_conversion_is_visible",
        "pg_database",
        "pg_depend",
        "pg_description",
        "pg_enum",
        "pg_function_is_visible",
        "pg_get_constraintdef",
        "pg_get_expr",
        "pg_get_viewdef",
        "pg_has_role",
        "pg_index",
        "pg_indexes",
        "pg_is_other_temp_schema",
        "pg_my_temp_schema",
        "pg_namespace",
        "pg_opclass_is_visible",
        "pg_operator_is_visible",
        "pg_opfamily_is_visible",
        "pg_postmaster_start_time",
        "pg_prepared_statements",
        "pg_proc",
        "pg_sequence",
        "pg_sequences",
        "pg_settings",
        "pg_size_pretty",
        "pg_sleep",
        "pg_table_is_visible",
        "pg_tables",
        "pg_tablespace",
        "pg_ts_config_is_visible",
        "pg_ts_dict_is_visible",
        "pg_ts_parser_is_visible",
        "pg_ts_template_is_visible",
        "pg_type",
        "pg_type_is_visible",
        "pg_typeof",
        "pg_views",
        "pragma_database_list",
        "shobj_description",
        "sqlite_master",
        "sqlite_schema",
        "sqlite_temp_master",
        "sqlite_temp_schema",
    }
)
ENGINE_NAMES_ALLOWED = frozenset({"information_schema", "current_database"})


# Functions that run SQL or open connections outside the lake; never allowed in a statement.
_EXTERNAL_SQL_FUNCTIONS = frozenset(
    {
        "postgres_query",
        "postgres_execute",
        "postgres_scan",
        "postgres_scan_pushdown",
        "postgres_attach",
        "sqlite_query",
        "sqlite_attach",
        "sqlite_scan",
        "query",
        "query_table",
        # Bind or run SQL given as text, on the lake's own connection: the text can name a file
        # no check of the statement sees (#136, Reviewer pass 9).
        "json_serialize_plan",
        "json_execute_serialized_sql",
    }
)


WRITE_STATEMENTS = frozenset(
    {
        duckdb.StatementType.CREATE,
        duckdb.StatementType.INSERT,
        duckdb.StatementType.UPDATE,
        duckdb.StatementType.DELETE,
        duckdb.StatementType.DROP,
        duckdb.StatementType.ALTER,
        duckdb.StatementType.MERGE_INTO,
    }
)


def _guarded(sql: str, table_functions: frozenset[str]) -> None:
    """Refuse SQL that names the catalog database or calls a table function other than generators.

    Table functions read files, run SQL or open connections (`read_text`, `query`,
    `postgres_execute`, …); only `range`, `generate_series` and `unnest` are allowed. DuckLake's
    table macros (`set_option`, `table_changes`, …) and table options (`CREATE TABLE … WITH
    (…)`, `ALTER TABLE … SET (…)`/`RESET (…)`) are refused too: the lake's options are the
    store's. Comments between a name and its `(` are skipped. The catalog database (in
    PostgreSQL the whole database, other lakes included) is only ever touched by DuckLake itself.
    The connection is also sandboxed to the lake's folders (`Isolation.sandbox`), so these checks
    are a first layer.
    """
    sql = _plain(sql)
    if "\x00" in sql:
        # DuckDB picks a reader from a whole string but opens the path only up to a NUL, which
        # Python's file checks refuse to look at (#136, Reviewer pass 8).
        raise StoreError(
            "STORE_STATEMENT_INVALID", "A statement cannot contain a NUL character.", {}
        )
    if METADATA_PREFIX in sql.lower():
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "Statements may not reference the lake's catalog database.",
            {},
        )
    every = _tokens(sql)
    tokens = [token for token in every if token[1] != duckdb.token_type.comment]
    # Each token ends where the next one starts (a comment included), or at the end of the
    # statement: looked up by position, so the checks stay linear in the statement's length.
    positions = [position for position, _ in every] + [len(sql)]
    ends = dict(itertools.pairwise(positions))
    _refuse_file_names(sql, tokens, ends)
    _refuse_show_statements(sql, tokens, ends)
    _refuse_time_travel(sql, tokens, ends)
    first = ""
    for index, (position, kind) in enumerate(tokens):
        if kind not in {duckdb.token_type.identifier, duckdb.token_type.keyword}:
            continue
        end = ends[position]
        name = sql[position:end].strip().strip('"').lower()
        following = tokens[index + 1][0] if index + 1 < len(tokens) else len(sql)
        first = first or name
        # A CTE always names itself before its `(`: `WITH (` is a table's options.
        if (
            sql[following : following + 1] == "("
            and kind == duckdb.token_type.keyword
            and (name == "with" or (first == "alter" and name in {"set", "reset"}))
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Table options are not allowed: the lake's options are set by the store.",
                {"clause": name.upper()},
            )
        if name in _ENGINE_NAMES:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "The engine's own catalog, logs and settings are not readable from SQL.",
                {"name": name},
            )
        if (
            sql[following : following + 1] == "("
            and name in DUCKLAKE_TABLE_MACROS
            and not _names_a_table(sql, tokens, ends, index)
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "DuckLake's own functions are not allowed: the store runs maintenance and sets "
                "the lake's options.",
                {"function": name},
            )
        if (
            sql[following : following + 1] == "("
            and (name in table_functions or name in _EXTERNAL_SQL_FUNCTIONS)
            and name not in READ_TABLE_FUNCTIONS
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Only the table functions range, generate_series and unnest are allowed; a "
                "function that shares its name with another table function (histogram) is "
                "refused in every form.",
                {"function": name},
            )


def _names_a_table(
    sql: str, tokens: list[tuple[int, duckdb.token_type]], ends: dict[int, int], index: int
) -> bool:
    """Whether the (possibly qualified) name at `index` follows `INTO`, `TABLE` or
    `REFERENCES`: a table with a column list (`INSERT INTO settings (a)`), not a call."""
    before = index - 1
    while before >= 1 and sql[tokens[before][0] : tokens[before][0] + 1] == ".":
        before -= 2  # `lake.main.settings`: skip each `name .`
    if before < 0:
        return False
    position, kind = tokens[before]
    end = ends[position]
    word = sql[position:end].strip().lower()
    return kind == duckdb.token_type.keyword and word in {"into", "table", "references"}


# Statement forms that read a table or file by name outside the lake's pinned reads (#153):
# `SUMMARIZE <name or file>`, `DESCRIBE`/`DESC <name>`, `SHOW …`.
_SHOW_STATEMENTS = frozenset({"summarize", "describe", "desc", "show"})
# Keywords that start a clause; `DESC` is an ordering only inside an `ORDER BY` clause.
_CLAUSES = frozenset(
    {
        "select",
        "from",
        "where",
        "group",
        "having",
        "qualify",
        "window",
        "order",
        "limit",
        "offset",
        "union",
        "intersect",
        "except",
        "values",
        "using",
        "on",
        "returning",
        "set",
    }
)


def _refuse_time_travel(
    sql: str, tokens: list[tuple[int, duckdb.token_type]], ends: dict[int, int]
) -> None:
    """Refuse a table read at another snapshot (`… AT (VERSION => n)`, `AT (TIMESTAMP => t)`):
    a model reads the current data, so rows deleted since cannot come back through it (#147,
    decision #182). A read is pinned to its snapshot by the store itself, after this check. A
    name or label `at` is not followed by `(VERSION =>` or `(TIMESTAMP =>` and stays allowed."""
    texts = [sql[position : ends[position]].strip().lower() for position, _ in tokens]
    for index in range(len(texts) - 3):
        if (
            texts[index] == "at"
            and texts[index + 1] == "("
            and texts[index + 2] in {"version", "timestamp"}
            and texts[index + 3] == "=>"
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A statement reads the current data: a table cannot be read at another "
                "snapshot (AT (VERSION => …) or AT (TIMESTAMP => …)).",
                {"clause": "AT"},
            )


def _refuse_show_statements(
    sql: str, tokens: list[tuple[int, duckdb.token_type]], ends: dict[int, int]
) -> None:
    """Refuse `SUMMARIZE`, `DESCRIBE`, `DESC` and `SHOW` everywhere except where DuckDB reads
    them as something else: a column label after `AS` (`SELECT a AS desc`), a part of a qualified
    name after `.` (`t.desc`), and `DESC` as an ordering inside an `ORDER BY` clause. DuckDB
    reads a quoted target of `SUMMARIZE` as a file, and these statement forms are not pinned to a
    snapshot (#153). The rule lists what is allowed, so a position not thought of is refused.
    Quoted names (`"describe"`) are ordinary names."""
    words: list[tuple[str, duckdb.token_type]] = []
    for position, kind in tokens:
        end = ends[position]
        words.append((sql[position:end].strip().lower(), kind))
    # `CREATE TABLE x AS <query>`: that `AS` starts a query, it does not introduce a label. It is
    # found in every statement, so empty statements before it (`;CREATE …`) do not hide it.
    query_as: set[int] = set()
    starts = [0] + [index + 1 for index, (word, _) in enumerate(words) if word == ";"]
    for start in starts:
        if start >= len(words) or words[start][0] != "create":
            continue
        depth = 0
        for index in range(start, len(words)):
            word = words[index][0]
            if word == ";":
                break
            depth += (word == "(") - (word == ")")
            if word == "as" and depth == 0:
                query_as.add(index)
                break
    clauses = [""]  # the current clause at each parenthesis depth
    for index, (word, kind) in enumerate(words):
        if word == "(":
            clauses.append("")
            continue
        if word == ")":
            if len(clauses) > 1:
                clauses.pop()
            continue
        if kind != duckdb.token_type.keyword:
            continue
        # The `FROM` of `IS [NOT] DISTINCT FROM` is a comparison, not a clause.
        if word in _CLAUSES and not (word == "from" and words[index - 1][0] == "distinct"):
            clauses[-1] = word
        if word not in _SHOW_STATEMENTS:
            continue
        label = index > 0 and words[index - 1][0] == "as" and index - 1 not in query_as
        # After `.`, the word is part of a qualified name (`t.desc`); DuckDB reads it as a name.
        qualified = index > 0 and words[index - 1][0] == "."
        ordering = word == "desc" and clauses[-1] == "order"
        if not (label or qualified or ordering):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "SUMMARIZE, DESCRIBE and SHOW are not allowed in statements.",
                {"statement": word.upper()},
            )


def _single_statement(sql: str, table_functions: frozenset[str]) -> duckdb.Statement:
    # Parsed first: SQL DuckDB cannot parse fails with the parser's own message, whatever the
    # checks would say (#158). Parsing runs nothing.
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise StoreError(
            "STORE_QUERY_FAILED", "The SQL could not be parsed.", {"duckdb": str(error)}
        ) from error
    _guarded(sql, table_functions)
    if len(statements) != 1:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "Exactly one SQL statement is required.",
            {"statements": str(len(statements))},
        )
    return statements[0]


READ_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
# Table functions whose name is also a scalar function, known to read nothing in either form:
# `repeat` generates rows, `version` reports DuckDB's version. Any other name that is a table
# function (`histogram`, whose table form reads a table or file named by any expression, and
# whatever an extension adds) is refused in every form, its scalar or aggregate form included.
SHARED_HARMLESS_FUNCTIONS = frozenset({"repeat", "version"})
# DuckLake's table macros, created in each lake schema on first use (so `duckdb_functions()`
# never lists them): they change the lake's options (`set_option` could turn on deletion
# vectors, which DuckLake writes unencrypted), set commit messages, merge files, or read deleted
# rows and old snapshots. Only the store calls DuckLake's maintenance and options.
DUCKLAKE_TABLE_MACROS = frozenset(
    {
        "set_option",
        "set_commit_message",
        "merge_adjacent_files",
        "options",
        "settings",
        "current_snapshot",
        "last_committed_snapshot",
        "snapshots",
        "table_info",
        "table_changes",
        "table_deletions",
        "table_insertions",
    }
)
TABLE_FUNCTIONS_SQL = (
    "SELECT string_agg(DISTINCT lower(function_name), '|') FROM duckdb_functions() "
    "WHERE function_type IN ('table', 'table_macro', 'pragma')"
)


def _created_kind_allowed(sql: str, statement: duckdb.Statement) -> bool:
    """CREATE, DROP and ALTER may only target tables and schemas (decided from the tokens)."""
    if statement.type not in {
        duckdb.StatementType.CREATE,
        duckdb.StatementType.DROP,
        duckdb.StatementType.ALTER,
    }:
        return True
    tokens = _tokens(sql)
    ends = [position for position, _ in tokens[1:]] + [len(sql)]
    words = [
        sql[position:end].strip().upper()
        for (position, kind), end in zip(tokens, ends, strict=True)
        if kind == duckdb.token_type.keyword
    ][:6]
    modifiers = {"OR", "REPLACE", "TEMP", "TEMPORARY", "PERSISTENT", "IF", "NOT", "EXISTS"}
    target = next((word for word in words[1:] if word not in modifiers), "")
    return target in {"TABLE", "SCHEMA"}
