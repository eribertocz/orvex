"""#130: the store's SQL checks read DuckDB's tokens by character, also after non-ASCII text.

`duckdb.tokenize` reports byte offsets; these tests put accented names and strings before the
token each check must find.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import Parameter, ParameterType
from orvex.store.errors import StoreError
from orvex.store.lake import _created_kind_allowed, _guarded, _tokens, _typed


def table_functions() -> frozenset[str]:
    """The same set the lake builds: table functions that are not also scalar functions."""
    connection = duckdb.connect()
    rows = connection.execute(
        "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
        "WHERE function_type IN ('table', 'table_macro', 'pragma') "
        "AND lower(function_name) NOT IN (SELECT lower(function_name) FROM duckdb_functions() "
        "WHERE function_type IN ('scalar', 'aggregate', 'macro'))"
    ).fetchall()
    connection.close()
    return frozenset(str(row[0]) for row in rows)


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
    ],
)
def test_a_file_named_in_any_quoting_form_is_refused(a_file: str, template: str) -> None:
    sql = template.replace("{f}", a_file)
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
    ],
)
def test_ordinary_statements_are_accepted(sql: str) -> None:
    _guarded(sql, table_functions())
