"""#130: the store's SQL checks read DuckDB's tokens by character, also after non-ASCII text.

`duckdb.tokenize` reports byte offsets; these tests put accented names and strings before the
token each check must find.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import Parameter, ParameterType
from orvex.store.errors import StoreError
from orvex.store.lake import (
    SHARED_HARMLESS_FUNCTIONS,
    TABLE_FUNCTIONS_SQL,
    _created_kind_allowed,
    _guarded,
    _single_statement,
    _tokens,
    _typed,
)


def table_functions() -> frozenset[str]:
    """The same set the lake builds: every table function but the harmless shared names."""
    connection = duckdb.connect()
    (row,) = connection.execute(TABLE_FUNCTIONS_SQL).fetchall()
    connection.close()
    return frozenset(str(row[0]).split("|")) - {""} - SHARED_HARMLESS_FUNCTIONS


def test_tokens_are_positioned_by_character() -> None:
    sql = "SELECT 'medición', \"año\" FROM t WHERE x <= $since"
    for position, _ in _tokens(sql):
        assert sql[position] != " "
    dollar = [position for position, _ in _tokens(sql) if sql[position] == "$"]
    assert dollar == [sql.index("$")]


@pytest.mark.parametrize(
    ("kind", "value"),
    [("DATE", "2026-10-05"), ("TIMESTAMPTZ", "2026-10-05T00:00:00+00:00"), ("VARCHAR", "ñ")],
)
def test_parameters_after_non_ascii_text_are_bound(kind: str, value: str) -> None:
    sql = 'SELECT "año", \'medición\' AS m FROM (SELECT 2026 AS "año") WHERE $p IS NOT NULL'
    parameter = Parameter(name="p", type=ParameterType(kind), value=value)
    typed = _typed(sql, [parameter])
    assert f"CAST($p AS {kind})" in typed
    assert typed.startswith("SELECT \"año\", 'medición' AS m")
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    assert connection.execute(typed, {"p": value}).fetchall() == [(2026, "medición")]
    connection.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'é' AS a, * FROM read_text('/etc/hostname')",
        "SELECT \"año\" FROM query('SELECT 1')",
        "SELECT 'ñññ' AS a FROM postgres_query('db', 'SELECT 1')",
    ],
)
def test_table_functions_are_refused_after_non_ascii_text(sql: str) -> None:
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


def test_file_names_are_refused_after_non_ascii_text(tmp_path: Path) -> None:
    secret = tmp_path / "other.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(StoreError) as refused:
        _guarded(f"SELECT 'ñandú' AS b, * FROM '{secret.as_posix()}'", table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


def test_created_kinds_are_read_after_non_ascii_text() -> None:
    def statement(sql: str) -> duckdb.Statement:
        (found,) = duckdb.extract_statements(sql)
        return found

    view = "CREATE /* año */ VIEW v AS SELECT 1"
    assert _created_kind_allowed(view, statement(view)) is False
    table = 'CREATE /* año */ TABLE "tabla_año" AS SELECT 1'
    assert _created_kind_allowed(table, statement(table)) is True


# --- #153: every quoting form, and no SUMMARIZE / DESCRIBE / SHOW ---------------------------


@pytest.fixture
def a_file(tmp_path: Path) -> str:
    target = tmp_path / "t.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    return target.as_posix()


@pytest.mark.parametrize(
    "template",
    [
        "SUMMARIZE $${f}$$",
        "SUMMARIZE $q${f}$q$",
        "SELECT * FROM (SUMMARIZE $${f}$$)",
        "CREATE TABLE x AS SUMMARIZE $${f}$$",
        "SELECT * FROM t WHERE a IN (SELECT a FROM (SUMMARIZE $${f}$$))",
        "SELECT $${f}$$ AS name FROM t",
        "SELECT E'{f}' AS name FROM t",
        "SELECT * FROM '{f}'",
        # Reviewer pass 1: a non-ASCII dollar-quote tag, and the positions of problem 1.
        "SELECT $ñ${f}$ñ$ AS name FROM t",
        "SELECT * FROM t UNION ALL BY NAME SUMMARIZE $ñ${f}$ñ$",
        "INSERT INTO x SUMMARIZE $ñ${f}$ñ$",
        "SELECT $tag_1${f}$tag_1$ AS name FROM t",
        # #136 Reviewer pass 5: file URIs, which DuckDB opens as local files.
        "CREATE TABLE leak AS SELECT * FROM 'file:{f}'",
        "CREATE TABLE leak AS SELECT * FROM 'file://{f}'",
        "CREATE TABLE leak AS SELECT * FROM 'file://localhost{f}'",
        "SELECT * FROM $$file:{f}$$",
        "CREATE TABLE leak AS SELECT * FROM 'file://localhost{f}*'",
        # #136 Reviewer pass 6: dotted names, which DuckDB joins into a file path.
        'SELECT * FROM "{stem}".csv',
        'SELECT * FROM "{stem}"."csv"',
        'SELECT * FROM "{stem}" . csv',
        'CREATE TABLE leak AS SELECT * FROM "{folder}/[t]".csv',
    ],
)
def test_a_file_named_in_any_quoting_form_is_refused(a_file: str, template: str) -> None:
    stem, folder = a_file.removesuffix(".csv"), a_file.rsplit("/", 1)[0]
    sql = template.replace("{f}", a_file).replace("{stem}", stem).replace("{folder}", folder)
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "sql",
    [
        "SUMMARIZE t",
        "DESCRIBE t",
        "DESC t",
        "SHOW TABLES",
        "SELECT * FROM (DESCRIBE t)",
        "WITH w AS (SUMMARIZE t) SELECT * FROM w",
        "CREATE TABLE x AS DESCRIBE t",
        # Reviewer pass 1: after a set operator, as an INSERT source, after a WITH list.
        "SELECT * FROM t UNION ALL BY NAME SUMMARIZE t",
        "SELECT * FROM t UNION SHOW TABLES",
        "SELECT * FROM t INTERSECT DESCRIBE t",
        "SELECT * FROM t EXCEPT DESC t",
        "INSERT INTO x SUMMARIZE t",
        "INSERT INTO x BY NAME SUMMARIZE t",
        "INSERT INTO x (a, b) DESCRIBE t",
        "WITH w AS (SELECT 1) SUMMARIZE w",
        "CREATE OR REPLACE TABLE x AS SHOW TABLES",
        # Reviewer pass 2: empty statements before CREATE do not hide its query AS.
        ";CREATE TABLE x AS SUMMARIZE t",
        "; CREATE TABLE x AS DESCRIBE t",
        ";;CREATE OR REPLACE TABLE x AS SHOW TABLES",
    ],
)
def test_summarize_describe_and_show_are_refused(sql: str) -> None:
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"
    assert "SUMMARIZE, DESCRIBE and SHOW" in refused.value.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$plain text$$ AS label",
        "SELECT 'it''s' AS a",
        "SELECT E'line\\n' AS b",
        'SELECT "summarize", "describe", "show" FROM t',
        'CREATE TABLE "describe" AS SELECT 1 AS x',
        "SELECT a FROM t ORDER BY a DESC",
        "SELECT row_number() OVER (ORDER BY a DESC) AS n FROM t",
        # Reviewer pass 1: labels after AS were accepted on main and stay accepted.
        "SELECT a AS desc FROM t",
        "SELECT 1 AS show, 2 AS summarize, 3 AS describe",
        "CREATE TABLE x AS SELECT a AS desc FROM t ORDER BY a DESC",
        "SELECT a FROM t ORDER BY (a) DESC NULLS LAST, b DESC",
        "SELECT string_agg(a, ',' ORDER BY b DESC) AS s FROM t",
        "SELECT * FROM t UNION ALL SELECT * FROM u ORDER BY 1 DESC",
        # Reviewer pass 2: a label is still a label after empty statements.
        ";CREATE TABLE x AS SELECT a AS desc FROM t",
        # Reviewer pass 3: qualified names, and the FROM of IS DISTINCT FROM.
        "SELECT t.desc FROM t",
        "SELECT t.show, s.desc FROM t",
        "SELECT x.summarize FROM (SELECT 1 AS summarize) x",
        "CREATE TABLE y AS SELECT t . desc FROM t",
        "SELECT a FROM t ORDER BY a IS DISTINCT FROM 1 DESC",
        "SELECT a FROM t ORDER BY a IS NOT DISTINCT FROM 1 DESC",
    ],
)
def test_ordinary_statements_are_accepted(sql: str) -> None:
    _guarded(sql, table_functions())


@pytest.mark.parametrize("escape", ["\\x2e", "\\056", "\\."])
def test_a_file_named_with_e_string_escapes_is_refused(a_file: str, escape: str) -> None:
    # Reviewer pass 2: `E'…'` is read with DuckDB's own escapes, so `\x2e` for `.` still names it.
    escaped = a_file.replace(".", escape)
    with pytest.raises(StoreError) as refused:
        _guarded(f"SELECT E'{escaped}' AS name FROM t", table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize("sql", ["SELECT E'a\\'", "SELECT E'\\x00' AS a"])
def test_a_malformed_e_string_fails_as_a_store_error(sql: str) -> None:
    # Reviewer pass 3: decoding `E'…'` must not let a raw DuckDB error escape.
    with pytest.raises(StoreError) as failed:
        _single_statement(sql, table_functions())
    assert failed.value.code == "STORE_QUERY_FAILED"


def test_a_file_in_the_working_folder_named_by_a_dotted_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `x.csv` unquoted: DuckDB reads `x.csv` in the working folder (#136, Reviewer pass 6).
    (tmp_path / "x.csv").write_text("a\n1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(StoreError) as refused:
        _guarded("CREATE TABLE leak AS SELECT * FROM x.csv", table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"
    _guarded("CREATE TABLE ok AS SELECT * FROM s.t", table_functions())  # an ordinary name


@pytest.mark.parametrize(
    "named",
    ["'{d}/**/*.parquet'", '"{d}/**/*".parquet', "'{d}/*/y.parquet'"],
    ids=["recursive", "recursive-dotted", "hidden-folder"],
)
def test_a_glob_duckdb_expands_further_than_python_is_refused(tmp_path: Path, named: str) -> None:
    # #136 Reviewer pass 7: DuckDB's `**` goes to any depth and its `*` matches hidden names.
    deep = tmp_path / "data" / "main" / "t"
    deep.mkdir(parents=True)
    (deep / "x.parquet").write_bytes(b"PAR1")
    hidden = tmp_path / "data" / ".h"
    hidden.mkdir()
    (hidden / "y.parquet").write_bytes(b"PAR1")
    sql = "CREATE TABLE leak AS SELECT * FROM " + named.replace(
        "{d}", (tmp_path / "data").as_posix()
    )
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "named",
    ["'{f}\x00.parquet'", '"{f}\x00".parquet', '"{f}\x00.parquet"'],
    ids=["string", "dotted", "quoted-name"],
)
def test_a_nul_character_cannot_cut_a_file_name(a_file: str, named: str) -> None:
    # #136 Reviewer pass 8: DuckDB opens a path only up to a NUL, after choosing its reader
    # from the whole string; Python's file checks do not look at such a path. (An escaped NUL,
    # `E'…\\x00…'`, is a parser error in DuckDB 2.0.0: it never names a file.)
    sql = "CREATE TABLE stolen AS FROM " + named.replace("{f}", a_file)
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE stolen AS SELECT json_serialize_plan('SELECT 1', optimize := true) AS p",
        "SELECT json_serialize_plan('SELECT * FROM ''' || 'x.parquet' || '''') AS p",
        "SELECT JSON_SERIALIZE_PLAN /* a comment */ ('SELECT 1') AS p",
        "SELECT * FROM json_execute_serialized_sql('{}')",
    ],
    ids=["write", "concatenated", "comment-and-case", "execute"],
)
def test_functions_that_bind_sql_text_are_refused(sql: str) -> None:
    # #136 Reviewer pass 9: json_serialize_plan binds its SQL text on the lake's connection,
    # where a file named in that text is opened.
    with pytest.raises(StoreError) as refused:
        _guarded(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "sql",
    [
        # Pass 10: the table form reads the table or file its first argument names, and the
        # name can be built by an expression no file-name check sees.
        "CREATE TABLE stolen AS SELECT * FROM histogram('da' || 'ta.parquet', name)",
        "CREATE TABLE stolen AS SELECT * FROM histogram('s3:/' || '/b/x.parquet', n, "
        "technique := 'sample')",
        "INSERT INTO t SELECT * FROM HISTOGRAM(concat('a', 'b'), n, bin_count := 1000)",
        "SELECT * FROM \"histogram\"('t', n)",
        "SELECT * FROM main.histogram('t', n)",
        # Its aggregate form shares the name, and a name alone cannot tell the two apart.
        "CREATE TABLE h AS SELECT histogram(n) AS h FROM t",
    ],
)
def test_histogram_is_refused_in_every_form(sql: str) -> None:
    with pytest.raises(StoreError) as raised:
        _guarded(sql, table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    assert raised.value.details == {"function": "histogram"}


def test_every_shared_name_but_the_harmless_ones_is_refused() -> None:
    """A table function that shares its name with a scalar or aggregate function is refused,
    unless it is known to read nothing; a new one (from DuckDB or an extension) is refused."""
    connection = duckdb.connect()
    shared = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type IN ('table', 'table_macro', 'pragma') "
            "AND lower(function_name) IN (SELECT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type IN ('scalar', 'aggregate', 'macro'))"
        ).fetchall()
    }
    connection.close()
    assert "histogram" in shared
    assert shared - SHARED_HARMLESS_FUNCTIONS - {"range", "generate_series"} <= table_functions()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE r AS SELECT repeat('x', 3) AS s",
        "CREATE TABLE r AS SELECT * FROM repeat(1, 3)",
        "CREATE TABLE v AS SELECT version() AS v",
    ],
)
def test_the_harmless_shared_names_stay_allowed(sql: str) -> None:
    _guarded(sql, table_functions())


@pytest.mark.parametrize(
    "sql",
    [
        # #136 Reviewer pass 11: DuckLake creates these in each lake schema on first use, so
        # `duckdb_functions()` does not list them.
        "CREATE TABLE x AS SELECT * FROM lake.set_option('write_deletion_vectors', true)",
        "CREATE TABLE x AS SELECT * FROM set_option('require_commit_message', true)",
        "INSERT INTO t SELECT * FROM lake.table_deletions('readings', 0, 1000)",
        "CREATE TABLE x AS SELECT * FROM lake.main.table_changes('readings', 0, 1000)",
        "CREATE TABLE x AS SELECT * FROM lake.settings()",
        "CREATE TABLE x AS SELECT * FROM lake.merge_adjacent_files()",
        "CREATE TABLE x AS SELECT * FROM lake.snapshots()",
        "CREATE TABLE x AS SELECT * FROM lake.\"SET_OPTION\" /* c */ ('a', 1)",
        "SELECT (SELECT count(*) FROM lake.set_option('write_deletion_vectors', true))",
    ],
)
def test_ducklake_table_macros_are_refused(sql: str) -> None:
    with pytest.raises(StoreError) as raised:
        _guarded(sql, table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    assert "function" in raised.value.details


@pytest.mark.parametrize(
    ("sql", "clause"),
    [
        ("CREATE TABLE t (a INT) WITH (write_deletion_vectors = true)", "WITH"),
        ("CREATE TABLE t WITH (write_deletion_vectors = true) AS SELECT 1 AS a", "WITH"),
        ("ALTER TABLE readings SET (write_deletion_vectors = true)", "SET"),
        ("ALTER TABLE readings RESET (write_deletion_vectors)", "RESET"),
        ("ALTER TABLE readings SET /* c */ (write_deletion_vectors = true)", "SET"),
    ],
)
def test_table_options_are_refused(sql: str, clause: str) -> None:
    with pytest.raises(StoreError) as raised:
        _guarded(sql, table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    assert raised.value.details == {"clause": clause}


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t AS WITH c AS (SELECT 1 AS a) SELECT * FROM c",
        "CREATE TABLE t AS WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c "
        "WHERE n < 3) SELECT * FROM c",
        "UPDATE t SET a = (SELECT 1)",
        "ALTER TABLE t ADD COLUMN settings INTEGER",
        "CREATE TABLE t AS SELECT 1 AS options, 2 AS snapshots",
        "INSERT INTO settings (a) VALUES (1)",
        'INSERT INTO lake.main."options" (a) SELECT 1',
        "CREATE TABLE snapshots (a INTEGER)",
        "CREATE TABLE t (a INTEGER REFERENCES settings (a))",
    ],
)
def test_ctes_updates_and_columns_named_like_the_macros_stay_allowed(sql: str) -> None:
    _guarded(sql, table_functions())


def wide(columns: int) -> str:
    """A generated wide model: one CASE expression with a string constant per column."""
    items = ", ".join(
        f"CASE WHEN a{i} > {i} THEN 'x{i}' ELSE b{i} END AS c{i}" for i in range(columns)
    )
    return f"CREATE TABLE t AS SELECT {items} FROM src"


def guard_seconds(sql: str) -> float:
    functions = table_functions()
    started = time.perf_counter()
    _guarded(sql, functions)
    return time.perf_counter() - started


def test_the_statement_checks_take_time_in_proportion_to_the_statement() -> None:
    # #158: each token's end was found by scanning every position, quadratic in the length
    # (116 KB took 15 s). About 1 MB is checked well within 2 s, 116 KB within 0.5 s.
    large = wide(18_000)
    assert len(large) > 1_000_000
    assert guard_seconds(large) < 2.0
    assert guard_seconds(wide(2_000)) < 0.5
    small, big = guard_seconds(wide(2_000)), guard_seconds(wide(8_000))
    assert big < 8 * small + 0.2, (small, big)
