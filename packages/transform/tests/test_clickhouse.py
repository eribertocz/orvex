"""P1-17 (#65): extraction-model rules read from the text, and the ClickHouse → DuckDB types."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.transform.clickhouse import (
    ScanError,
    evaluated_while_describing,
    map_type,
    scan,
    since_problems,
    since_type,
)
from orvex.transform.models import Model, discover_models

CONNECTION = ClickhouseConnection.model_validate(
    {
        "type": "clickhouse",
        "host": "localhost",
        "port": 8123,
        "database": "default",
        "user": "reader",
        "password": "${env:CH_PASSWORD}",
        "read_only": True,
    }
)
INCREMENTAL = (
    "version: 1\nconnection: ch\nmaterialized: incremental\nincremental:\n"
    "  strategy: merge\n  unique_key: [event_id]\n  time_column: event_time\n  filter: manual\n"
)
EXAMPLE = (
    "SELECT event_id, meter_id, event_time, toDecimal64(value, 4) AS value\nFROM events\n"
    "WHERE event_time > {since:DateTime64(6, 'UTC')}"
)


def model(tmp_path: Path, sql: str, sidecar: str = INCREMENTAL) -> Model:
    (tmp_path / "models" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models" / "raw" / "ch_events.sql").write_text(sql, encoding="utf-8")
    (tmp_path / "models" / "raw" / "ch_events.yaml").write_text(sidecar, encoding="utf-8")
    found = discover_models(tmp_path, {"ch": CONNECTION})
    assert found.problems == ()
    (only,) = found.models
    return only


def codes(tmp_path: Path, sql: str, sidecar: str = INCREMENTAL) -> list[str]:
    return [item.code.root for item in since_problems(model(tmp_path, sql, sidecar))]


def test_the_example_model_passes(tmp_path: Path) -> None:
    assert codes(tmp_path, EXAMPLE) == []
    assert codes(tmp_path / "b", EXAMPLE + ";\n-- end\n") == []  # a final `;` is allowed


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT 1; SELECT 2", "INVALID_MODEL_SQL"),
        ("SELECT 1 AS a WHERE '{since:Date}' = '' -- {since:Date}", "MISSING_SINCE_PARAMETER"),
        ("SELECT 1 /* {since:Date} */ AS a", "MISSING_SINCE_PARAMETER"),
        ("SELECT 1 AS `{since:Date}`", "MISSING_SINCE_PARAMETER"),
        ("SELECT 1 AS a # {since:Date}", "MISSING_SINCE_PARAMETER"),
        ("SELECT $$ {since:Date} $$ AS a", "MISSING_SINCE_PARAMETER"),
        ("SELECT $1$ {since:Date} $1$ AS a", "MISSING_SINCE_PARAMETER"),
        ("SELECT $tag_2$ {since:Date} $tag_2$ AS a", "MISSING_SINCE_PARAMETER"),
        ("SELECT 1 WHERE t > { since:Date}", "MISSING_SINCE_PARAMETER"),  # not a parameter
        # A line break in a quoted name could forge a line of ClickHouse's EXPLAIN AST output.
        ("SELECT 1 AS `a\n   TableExpression x`", "INVALID_MODEL_SQL"),
        ("SELECT 1 AS `a\\n   TableExpression x`", "INVALID_MODEL_SQL"),
        ('SELECT 1 AS "a\u2028b"', "INVALID_MODEL_SQL"),
        ("SELECT 1 AS `a\x1cb`", "INVALID_MODEL_SQL"),
        # `$` inside an unquoted name does not start a heredoc that would hide a quoted name.
        ("SELECT 1 AS a$1$, 2 AS `x\n   TableExpression y`, 3 AS b$1$", "INVALID_MODEL_SQL"),
        ("SELECT 1 WHERE t > {since:String}", "MODEL_CONFIG_INVALID"),
        ("SELECT 1 WHERE t > {since: Date} AND u > {since:Date32}", "MODEL_CONFIG_INVALID"),
        ("SELECT 1 WHERE t > {since:Date} AND k = {key:String}", "INVALID_MODEL_SQL"),
        ("SELECT 'unterminated", "INVALID_MODEL_SQL"),
        ("", "INVALID_MODEL_SQL"),
    ],
)
def test_text_rules(tmp_path: Path, sql: str, code: str) -> None:
    assert codes(tmp_path, sql) == [code]


def test_a_table_model_must_not_use_since(tmp_path: Path) -> None:
    table = "version: 1\nconnection: ch\n"
    assert codes(tmp_path, "SELECT * FROM events WHERE t > {since:Date}", table) == [
        "INVALID_MODEL_SQL"
    ]
    assert codes(tmp_path / "b", "SELECT * FROM events", table) == []


def test_scan() -> None:
    found = scan("SELECT {since:DateTime64(6, 'America/La_Paz')} AS s, ';' AS t;")
    assert [(p.name, p.type) for p in found.parameters] == [
        ("since", "DateTime64(6, 'America/La_Paz')")
    ]
    assert found.statements == 1
    assert found.text == "SELECT {since:DateTime64(6, 'America/La_Paz')} AS s, ';' AS t"
    assert scan("SELECT {'a': 1} AS m").parameters == ()  # a map literal is not a parameter
    assert scan("SELECT 'it''s', \"a\"\"b\", 'x\\'y' AS z").statements == 1
    assert scan("/* a /* nested */ ; */ SELECT 1").statements == 1
    with pytest.raises(ScanError):
        scan("SELECT 1 /* open")


@pytest.mark.parametrize(
    ("text", "lowest"),
    [
        ("Date", "1970-01-01"),
        ("Date32", "1900-01-01"),
        ("DateTime", "1970-01-01 00:00:00"),
        ("DateTime('UTC')", "1970-01-01 00:00:00"),
        ("DateTime64(3)", "1900-01-01 00:00:00"),
        ("DateTime64(6, 'America/La_Paz')", "1900-01-01 00:00:00"),
    ],
)
def test_since_types(text: str, lowest: str) -> None:
    declared = since_type(text)
    assert declared is not None
    assert declared.lowest() == lowest


@pytest.mark.parametrize("text", ["String", "DateTime64", "DateTime64(10)", "Nullable(Date)"])
def test_since_types_refused(text: str) -> None:
    assert since_type(text) is None


@pytest.mark.parametrize(
    ("clickhouse", "duckdb", "nullable"),
    [
        ("Int8", "TINYINT", False),
        ("Int16", "SMALLINT", False),
        ("Int32", "INTEGER", False),
        ("Int64", "BIGINT", False),
        ("Int128", "HUGEINT", False),
        ("UInt8", "UTINYINT", False),
        ("UInt16", "USMALLINT", False),
        ("UInt32", "UINTEGER", False),
        ("UInt64", "UBIGINT", False),
        ("UInt128", "UHUGEINT", False),
        ("Decimal(18, 4)", "DECIMAL(18,4)", False),
        ("Decimal(38, 10)", "DECIMAL(38,10)", False),
        ("Float32", "FLOAT", False),
        ("Float64", "DOUBLE", False),
        ("String", "VARCHAR", False),
        ("FixedString(16)", "VARCHAR", False),
        ("LowCardinality(String)", "VARCHAR", False),
        ("LowCardinality(Nullable(String))", "VARCHAR", True),
        ("Enum8('a' = 1, 'b' = 2)", "VARCHAR", False),
        ("Enum16('it''s' = -3)", "VARCHAR", False),
        ("Bool", "BOOLEAN", False),
        ("UUID", "UUID", False),
        ("Date", "DATE", False),
        ("Date32", "DATE", False),
        ("DateTime", "TIMESTAMPTZ", False),
        ("DateTime('UTC')", "TIMESTAMPTZ", False),
        ("DateTime64(3)", "TIMESTAMPTZ", False),
        ("DateTime64(6, 'America/La_Paz')", "TIMESTAMPTZ", False),
        ("Nullable(UInt64)", "UBIGINT", True),
        ("Nullable(Decimal(18, 4))", "DECIMAL(18,4)", True),
        ("Nullable(DateTime64(6, 'UTC'))", "TIMESTAMPTZ", True),
    ],
)
def test_type_mapping(clickhouse: str, duckdb: str, nullable: bool) -> None:
    mapped = map_type(clickhouse)
    assert mapped is not None
    assert (mapped.duckdb, mapped.nullable) == (duckdb, nullable)


def test_time_zones_are_kept() -> None:
    zoned = map_type("DateTime64(6, 'America/La_Paz')")
    assert zoned is not None
    assert zoned.zone == "America/La_Paz"
    plain = map_type("DateTime")
    assert plain is not None
    assert plain.zone is None  # the server's zone


@pytest.mark.parametrize(
    "clickhouse",
    [
        "Int256",
        "UInt256",
        "Decimal(76, 10)",
        "Decimal(39, 0)",
        "DateTime64(9)",
        "DateTime64(7, 'UTC')",
        "Array(Int32)",
        "Map(String, UInt64)",
        "Tuple(a UInt8, b String)",
        "JSON",
        "Point",
        "Nothing",
        "Nullable(Nothing)",
        "IPv4",
        "LowCardinality(UInt8)",
        "Nullable(LowCardinality(String))",
        "Variant(String, UInt64)",
        "Decimal(18,",
    ],
)
def test_unsupported_types(clickhouse: str) -> None:
    assert map_type(clickhouse) is None


def ast(*lines: str) -> str:
    """An `EXPLAIN AST` text from node lines, with each node's `(children N)` filled in."""
    bare = [re.sub(r" \(children [0-9]+\)$", "", line) for line in lines]
    depths = [len(line) - len(line.lstrip(" ")) for line in bare]
    out = []
    for index, line in enumerate(bare):
        children = 0
        for later in depths[index + 1 :]:
            if later <= depths[index]:
                break
            children += later == depths[index] + 1
        out.append(f"{line} (children {children})" if children else line)
    return "\n".join(out)


SELECT_HEAD = (
    "SelectWithUnionQuery (children 1)",
    " ExpressionList (children 1)",
    "  SelectQuery (children 3)",
)


def test_subqueries_clickhouse_evaluates_while_describing() -> None:
    from_and_in = ast(
        *SELECT_HEAD,
        "   TablesInSelectQuery (children 1)",
        "    TablesInSelectQueryElement (children 1)",
        "     TableExpression (children 1)",
        "      Subquery (alias t) (children 1)",
        "       SelectWithUnionQuery (children 1)",
        "   Function in (children 1)",
        "    ExpressionList (children 2)",
        "     Identifier a",
        "     Subquery (children 1)",
        "      SelectWithUnionQuery (children 1)",
    )
    assert not evaluated_while_describing(from_and_in)
    named = ast(
        *SELECT_HEAD,
        "   ExpressionList (children 1)",
        "    WithElement (children 1)",
        "     Subquery (children 1)",
    )
    assert not evaluated_while_describing(named)
    scalar = ast(
        *SELECT_HEAD,
        "   ExpressionList (children 2)",
        "    Identifier a",
        "    Subquery (alias s) (children 1)",
    )
    assert evaluated_while_describing(scalar)
    left_of_in = ast(
        *SELECT_HEAD,
        "   Function in (children 1)",
        "    ExpressionList (children 2)",
        "     Subquery (children 1)",
        "      SelectWithUnionQuery (children 1)",
        "     Literal UInt64_1",
    )
    assert evaluated_while_describing(left_of_in)
    constant_lookup = ast(
        *SELECT_HEAD,
        "   ExpressionList (children 2)",
        "    Function dictGet (alias g) (children 1)",
        "     ExpressionList (children 3)",
        "      Literal \\'d\\'",
        "      Literal \\'v\\'",
        "      Function toUInt64 (children 1)",
        "       ExpressionList (children 1)",
        "        Literal UInt64_5",
    )
    # Dictionary lookups are allowed (decision #114); the row limit is ClickHouse's guard.
    assert not evaluated_while_describing(constant_lookup)
    # A line whose node announces other children than it has, or a second root, fails closed.
    assert evaluated_while_describing(named.replace("WithElement (children 1)", "WithElement"))
    assert evaluated_while_describing(
        named.replace("WithElement (children 1)", "WithElement (children 2)")
    )
    assert evaluated_while_describing(named + "\nSelectWithUnionQuery")
    replace = ast(
        "SelectWithUnionQuery (children 1)",
        " ExpressionList (children 1)",
        "  SelectQuery (children 1)",
        "   ExpressionList (children 1)",
        "    Asterisk (children 1)",
        "     ColumnsTransformerList (children 1)",
        "      ColumnsReplaceTransformer (children 1)",
        "       ColumnsReplaceTransformer::Replacement (children 1)",
        "        Identifier a",
    )
    assert not evaluated_while_describing(replace)
    # Text that is not a tree of node lines fails closed.
    assert evaluated_while_describing(named + "\n   x not a node")
    assert evaluated_while_describing(named.replace("    WithElement", "      WithElement"))
    exists = ast(
        *SELECT_HEAD,
        "   Function exists (children 1)",
        "    ExpressionList (children 1)",
        "     Subquery (children 1)",
    )
    assert evaluated_while_describing(exists)
