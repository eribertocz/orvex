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
