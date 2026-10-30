"""#130: the store's SQL checks read DuckDB's tokens by character, also after non-ASCII text.

`duckdb.tokenize` reports byte offsets; these tests put accented names and strings before the
token each check must find.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import Parameter, ParameterType
from orvex.store.errors import StoreError
from orvex.store.lake import (
    PARSER_SPACES,
    SHARED_HARMLESS_FUNCTIONS,
    TABLE_FUNCTIONS_SQL,
    _created_kind_allowed,
    _guarded,
    _plain,
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


# --- #178: the checks cost a bounded amount --------------------------------------------------


def test_many_e_strings_are_checked_quickly() -> None:
    sql = "SELECT " + ", ".join(f"E'v\\x{index % 10}a' AS c{index}" for index in range(2000))
    started = time.monotonic()
    _guarded(sql, table_functions())
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("pattern", ["/**/zz_none*", "/usr/**/zz_none*"])
def test_a_pattern_too_broad_to_check_is_refused_quickly(pattern: str) -> None:
    started = time.monotonic()
    with pytest.raises(StoreError) as raised:
        _guarded(f"SELECT '{pattern}' AS p", table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    assert time.monotonic() - started < 1.0
    # #193: `/**` may meet a folder the user cannot list before the budget runs out (the CI
    # runner does); `/usr` holds none.
    reasons = (
        {"pattern_too_broad", "pattern_unreadable"}
        if pattern == "/**/zz_none*"
        else {"pattern_too_broad"}
    )
    assert raised.value.details["reason"] in reasons


def test_many_strings_over_one_large_folder_are_bounded(tmp_path: Path) -> None:
    # #193 Reviewer pass 1: 34,000 'big/zz*' over a kept listing of 9,000 names took 104 s.
    big = tmp_path / "big"
    big.mkdir()
    for index in range(9000):
        (big / f"f{index}").write_text("")
    sql = "SELECT " + ", ".join(f"'{big}/zz*' AS c{index}" for index in range(34000))
    started = time.monotonic()
    with pytest.raises(StoreError) as raised:
        _guarded(sql, table_functions())
    assert time.monotonic() - started < 1.0
    assert raised.value.details == {"reason": "pattern_too_broad"}


def test_a_pattern_in_a_small_folder_is_checked_as_before(tmp_path: Path) -> None:
    (tmp_path / "data" / ".hidden").mkdir(parents=True)
    (tmp_path / "data" / ".hidden" / "x.csv").write_text("a\n1\n")
    (tmp_path / "empty").mkdir()
    for pattern in (f"{tmp_path}/data/**/*.csv", f"{tmp_path}/data/*/x.csv"):
        with pytest.raises(StoreError):
            _guarded(f"SELECT '{pattern}' AS p", table_functions())
    _guarded(f"SELECT '{tmp_path}/empty/**/*.csv' AS p", table_functions())  # nothing matches


def test_one_budget_covers_the_whole_statement(tmp_path: Path) -> None:
    folders = []
    for number in range(50):
        folder = tmp_path / f"many{number}"
        folder.mkdir()
        for index in range(1000):
            (folder / f"f{index}.txt").write_text("")
        folders.append(folder)
    single = f"SELECT '{folders[0]}/*.csv' AS p"
    _guarded(single, table_functions())  # 1,000 entries: within the budget
    # #193: the same folder read by 50 patterns is read once.
    same = "SELECT " + ", ".join(f"'{folders[0]}/*.csv{index}' AS p{index}" for index in range(50))
    _guarded(same, table_functions())
    # 50 folders of 1,000 entries together: past it.
    together = "SELECT " + ", ".join(
        f"'{folder}/*.csv' AS p{index}" for index, folder in enumerate(folders)
    )
    with pytest.raises(StoreError) as raised:
        _guarded(together, table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    assert raised.value.details == {"reason": "pattern_too_broad"}


@pytest.mark.parametrize(
    "shape",
    [
        "{folder}/" + "**/" * 900 + "zz",
        "/" * 3000 + "zz*",
        "/u*" + "/" * 3000 + "x",
        "/zz_none/" + "**/" * 2000 + "x",
    ],
    ids=[
        "many-double-stars",
        "many-slashes",
        "slashes-after-a-pattern",
        "double-stars-below-nothing",
    ],
)
def test_a_long_pattern_is_checked_quickly_without_error(tmp_path: Path, shape: str) -> None:
    # #178 Reviewer pass 1: these took seconds, or raised RecursionError.
    for index in range(10):
        (tmp_path / f"s{index}").mkdir()
    sql = "SELECT '" + shape.format(folder=tmp_path) + "' AS p"
    started = time.monotonic()
    _guarded(sql, table_functions())  # nothing matches: allowed, and no other error
    assert time.monotonic() - started < 1.0


def test_each_folder_read_costs_more_than_an_entry(tmp_path: Path) -> None:
    # #178: a cold disk cache makes each folder read slow (about 2 ms on the CI runner), so
    # folders are counted apart from the entries they hold: 250 empty folders pass the budget.
    for index in range(250):
        (tmp_path / f"d{index}").mkdir()
    with pytest.raises(StoreError) as raised:
        _guarded(f"SELECT '{tmp_path}/*/zz*' AS p", table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"
    for index in range(150, 250):
        (tmp_path / f"d{index}").rmdir()
    _guarded(f"SELECT '{tmp_path}/*/zz*' AS p", table_functions())  # 150 folders: within it


def test_text_naming_missing_folders_costs_little() -> None:
    # A missing folder costs one entry, once per statement: ordinary text such as 'N/A*' stays
    # allowed (#193).
    sql = "SELECT " + ", ".join(f"'N/A*{index}' AS c{index}" for index in range(5000))
    _guarded(sql, table_functions())


def test_text_that_lists_the_working_folder_reads_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #193: text with a `.` and a glob character but no `/` lists the working folder; about 140
    # such strings were refused when each one read it again.
    for index in range(20):
        (tmp_path / f"w{index}.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    texts = [f"'Is it paid? Yes {index}.'" for index in range(1000)]
    texts += [f"'v1.{index}*'" for index in range(1000)]
    sql = "SELECT " + ", ".join(f"{text} AS c{index}" for index, text in enumerate(texts))
    started = time.monotonic()
    _guarded(sql, table_functions())
    assert time.monotonic() - started < 1.0


def test_a_named_file_and_an_unchecked_pattern_have_their_own_reason(tmp_path: Path) -> None:
    (tmp_path / "x.csv").write_text("a\n1\n")
    with pytest.raises(StoreError) as raised:
        _guarded(f"SELECT '{tmp_path}/x.csv' AS p", table_functions())
    assert raised.value.details == {"reason": "names_a_file"}
    # 250 folders pass the budget (`/**` would meet an unreadable folder first when not root).
    for index in range(250):
        (tmp_path / "wide" / f"d{index}").mkdir(parents=True)
    with pytest.raises(StoreError) as raised:
        _guarded(f"SELECT '{tmp_path}/wide/*/zz*' AS p", table_functions())
    assert raised.value.details == {"reason": "pattern_too_broad"}
    hidden = tmp_path / "closed"
    hidden.mkdir()
    hidden.chmod(0)
    try:
        if os.access(hidden, os.R_OK):
            pytest.skip("root reads any folder")
        with pytest.raises(StoreError) as raised:
            _guarded(f"SELECT '{hidden}/*.csv' AS p", table_functions())
        assert raised.value.details == {"reason": "pattern_unreadable"}
    finally:
        hidden.chmod(0o700)


def test_two_statements_do_not_share_their_listings(tmp_path: Path) -> None:
    # #193: a file created between two statements is seen by the second.
    sql = f"SELECT '{tmp_path}/*.csv' AS p"
    _guarded(sql, table_functions())
    (tmp_path / "late.csv").write_text("a\n1\n")
    with pytest.raises(StoreError) as raised:
        _guarded(sql, table_functions())
    assert raised.value.details == {"reason": "names_a_file"}


@pytest.mark.parametrize("length", [280, 5000])
def test_text_whose_folder_name_is_too_long_to_exist_is_allowed(length: int) -> None:
    # #178 Reviewer pass 3: ordinary text with a `/` and a `?` was refused when a part before
    # the `/` was longer than a file name can be.
    words = "x" * length
    sql = f"SELECT CASE WHEN a > 0 THEN '{words} and/or estimated?' END AS note FROM m"
    _guarded(sql, table_functions())


def test_a_symbolic_link_loop_is_refused_not_crashed(tmp_path: Path) -> None:
    (tmp_path / "loop").symlink_to(".")
    with pytest.raises(StoreError) as raised:
        _guarded(f"SELECT '{tmp_path}/**/zz*' AS p", table_functions())
    assert raised.value.code == "STORE_STATEMENT_INVALID"


def test_a_pattern_under_the_home_folder_is_expanded_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a\n1\n")
    for named in ("~/data/x.csv", "~/data/*.csv", "~/**/x.csv"):
        with pytest.raises(StoreError):
            _guarded(f"SELECT '{named}' AS p", table_functions())
    _guarded("SELECT '~/data/*.parquet' AS p", table_functions())  # nothing matches


def test_parser_spaces_are_every_character_duckdb_reads_as_whitespace() -> None:
    # #213: measured over every code point; a DuckDB upgrade that changes the set fails here.
    found = set()
    for point in range(0x80, 0x110000):
        if 0xD800 <= point <= 0xDFFF:
            continue
        try:
            duckdb.extract_statements(f"SELECT{chr(point)}1")
        except duckdb.Error:
            continue
        found.add(chr(point))
    assert found == set(PARSER_SPACES)


_UNICODE_SPACE_BYPASSES = [
    "CREATE TABLE x AS SELECT * FROM{c}read_text('x')",
    "CREATE TABLE x AS SELECT * FROM read_text{c}('x')",
    "CREATE TABLE x AS SELECT * FROM{c}query('SELECT 1')",
    "SELECT * FROM{c}read_text('x')",
]


@pytest.mark.parametrize("space", list(PARSER_SPACES), ids=lambda c: f"U+{ord(c):04X}")
def test_names_next_to_a_unicode_space_are_refused(space: str) -> None:
    # #213: DuckDB reads these characters as spaces, `duckdb.tokenize` glued them to the name.
    for template in _UNICODE_SPACE_BYPASSES:
        sql = template.format(c=space)
        with pytest.raises(StoreError) as refused:
            _single_statement(sql, table_functions())
        assert refused.value.code == "STORE_STATEMENT_INVALID", sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'a\u00a0b' AS v",
        "SELECT E'a\u00a0b\\t' AS v",
        "SELECT $$a\u00a0b$$ AS v",
        "SELECT $q$a\u3000b$q$ AS v",
        'SELECT 1 AS "a\u00a0b"',
        "SELECT 'x\u00a0' AS v",
    ],
)
def test_strings_and_quoted_names_keep_their_unicode_spaces(sql: str) -> None:
    # #213: only the spaces between tokens are rewritten; values and names stay as written.
    # A space before `AS`: DuckDB reads a `$` followed by a non-ASCII byte as a new dollar tag.
    spaced = sql.replace("SELECT ", "SELECT\u2003", 1).replace(" AS", " \ufeffAS", 1)
    plain = _plain(spaced)
    assert len(plain) == len(spaced)
    connection = duckdb.connect()
    expected = connection.execute(sql)
    rows, names = expected.fetchall(), [column[0] for column in expected.description]
    result = connection.execute(plain)
    assert result.fetchall() == rows
    assert [column[0] for column in result.description] == names
    connection.close()


def test_a_comment_or_unquoted_text_has_its_unicode_spaces_rewritten() -> None:
    sql = "SELECT\u00a01 /*\u3000*/\u200bAS\u2060a"
    assert _plain(sql) == "SELECT 1 /* */ AS a"
    assert _plain("SELECT 1") == "SELECT 1"


@pytest.mark.parametrize(
    "sql", ["SELECT\u00a01 AS a", "CREATE TABLE t\u3000(a INTEGER)", "SELECT * FROM t\u200bAS u"]
)
def test_statements_with_unicode_spaces_are_still_accepted(sql: str) -> None:
    _single_statement(sql, table_functions())


def test_a_file_named_after_a_unicode_space_is_refused(a_file: str) -> None:
    with pytest.raises(StoreError) as refused:
        _single_statement(f"SELECT * FROM\u00a0'{a_file}'", table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


_DESYNCS = [
    # (first select item, whether DuckDB's prepass then rewrites the U+00A0 in the file name)
    ("/*'*/ 1", True),
    ("/* it's */ 1", True),
    ("E'\\''", True),
    ('/*"*/ 1', False),
    ("1 -- '\n", False),
    ("$t$'$t$", False),
    ('$$"$$', False),
]


@pytest.mark.parametrize(("before", "rewritten"), _DESYNCS)
def test_a_file_named_after_a_quote_duckdb_tracks_differently_is_refused(
    tmp_path: Path, before: str, rewritten: bool
) -> None:
    # Reviewer pass 1 on #214: DuckDB's prepass tracks quotes without block comments or
    # backslash escapes, so after `/*'*/` it rewrites the U+00A0 inside the next string; the
    # guard must check the name DuckDB opens (`lake dir`), which names an existing file.
    folder = tmp_path / "lake dir"
    folder.mkdir()
    (folder / "s.csv").write_text("a\n42\n", encoding="utf-8")
    named = (tmp_path / "lake\u00a0dir" / "s.csv").as_posix()
    sql = f"CREATE TABLE x AS SELECT {before} AS one, * FROM '{named}'"
    connection = duckdb.connect()
    try:
        opened = connection.execute(sql.replace("CREATE TABLE x AS ", "")).fetchall()
    except duckdb.IOException:
        opened = []  # DuckDB kept the U+00A0: no file of that name
    connection.close()
    assert bool(opened) is rewritten  # pins DuckDB's prepass
    if not opened:
        _single_statement(sql, table_functions())  # nothing to refuse: no such file
        return
    with pytest.raises(StoreError) as refused:
        _single_statement(sql, table_functions())
    assert refused.value.code == "STORE_STATEMENT_INVALID"


_FRAGMENTS = [
    "/*'*/",
    '/*"*/',
    "E'\\''",
    "-- '\n",
    "$t$'$t$",
    "'a\u00a0b'",
    "'c\u3000d'",
    "\u00a0",
    "\u200b",
    " ",
    "$$x\u2003y$$",
    "'it''s\u00a0'",
]


def test_the_text_checked_is_what_duckdb_runs() -> None:
    # #213: `_plain` reproduces DuckDB's prepass, so DuckDB reads each string constant exactly as
    # the guard does: the value it returns is spelled, quoted, in the checked text.
    import itertools

    connection = duckdb.connect()
    for first, second, third in itertools.product(_FRAGMENTS, repeat=3):
        sql = f"SELECT {first} {second} 'v\u00a0w' AS v {third}"
        plain = _plain(sql)
        assert _plain(plain) == plain, sql
        try:
            expected = connection.execute(sql).fetchall()
        except duckdb.Error:
            continue
        assert connection.execute(plain).fetchall() == expected, sql
        value = expected[0][-1] if expected else None
        if isinstance(value, str) and value.startswith("v"):
            assert f"'{value}'" in plain, (sql, plain, value)
    connection.close()


def test_the_last_two_bytes_are_left_as_duckdb_leaves_them() -> None:
    # DuckDB's prepass stops two bytes before the end: a final U+00A0 stays part of the name.
    sql = "SELECT 1 AS a\u00a0"
    assert _plain(sql) == sql
    connection = duckdb.connect()
    assert connection.execute(sql).description[0][0] == "a\u00a0"
    connection.close()


def test_a_long_dollar_quoted_string_is_rewritten_in_linear_time() -> None:
    # Reviewer pass 2 on #214: comparing the closing tag by slicing copied it at every `$`, so a
    # 1.6 MB statement took about 5 s; DuckDB parses it in a fraction of a second.
    size = 400_000
    sql = "SELECT \u00a0$" + "a" * size + "$" + "$" * (2 * size) + "$" + "a" * size + "$"
    started = time.monotonic()
    _plain(sql)
    assert time.monotonic() - started < 1.0


def test_a_lone_surrogate_is_a_parse_failure() -> None:
    with pytest.raises(StoreError) as raised:
        _single_statement(_plain("SELECT 'a\ud800'"), table_functions())
    assert raised.value.code == "STORE_QUERY_FAILED"
