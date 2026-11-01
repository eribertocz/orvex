"""P2-04b (#288): the OSSIE_SQL_2026 parser, construct by construct (`expression_language.md`
at `c721ffc`). Each valid construct gives a tree with its references and aggregate class; each
invalid text is a syntax error; recognizable SQL outside the subset is unsupported."""

from __future__ import annotations

import pytest
from orvex.semantic.ossie import sql


def refs(text: str) -> list[tuple[str, ...]]:
    return [ref.parts for ref in sql.references(sql.parse(text))]


@pytest.mark.parametrize(
    ("text", "references", "aggregate"),
    [
        ("amount", [("AMOUNT",)], None),
        ('"Amount"', [("Amount",)], None),
        ("o.amount", [("O", "AMOUNT")], None),
        ("SUM(amount)", [("AMOUNT",)], "distributive"),
        ("COUNT(*)", [], "distributive"),
        ("COUNT(DISTINCT customer_id)", [("CUSTOMER_ID",)], "holistic"),
        ("MAX(DISTINCT amount)", [("AMOUNT",)], "distributive"),
        ("AVG(amount)", [("AMOUNT",)], "algebraic"),
        ("SUM(a) / SUM(b)", [("A",), ("B",)], "distributive"),
        ("SUM(a) + MEDIAN(b)", [("A",), ("B",)], "holistic"),
        ("APPROX_COUNT_DISTINCT(id)", [("ID",)], "sketch"),
        ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount)", [("AMOUNT",)], "holistic"),
        ("SUM(amount) FILTER (WHERE status = 'paid')", [("AMOUNT",), ("STATUS",)], "distributive"),
        ("CASE WHEN a > 1 THEN 'x' ELSE 'y' END", [("A",)], None),
        ("CASE status WHEN 'a' THEN 1 END", [("STATUS",)], None),
        ("CAST(quantity AS DECIMAL(20, 0)) * price", [("QUANTITY",), ("PRICE",)], None),
        ("TRY_CAST(code AS INTEGER)", [("CODE",)], None),
        ("a BETWEEN 1 AND 10", [("A",)], None),
        ("a NOT IN (1, 2, 3)", [("A",)], None),
        ("name LIKE 'a%' ESCAPE '!'", [("NAME",)], None),
        ("name ILIKE 'a%'", [("NAME",)], None),
        ("a IS NOT NULL AND b IS DISTINCT FROM c", [("A",), ("B",), ("C",)], None),
        ("EXTRACT(YEAR FROM d)", [("D",)], None),
        ("DATE_TRUNC(MONTH, d)", [("D",)], None),
        ("DATE_TRUNC('MONTH', d)", [("D",)], None),
        ("POSITION('x' IN name)", [("NAME",)], None),
        ("CURRENT_DATE", [], None),
        ("COALESCE(a, 0) || '-' || b", [("A",), ("B",)], None),
        ("-amount % 3", [("AMOUNT",)], None),
        ("NOT (a = 1 OR b <> 2)", [("A",), ("B",)], None),
        ("SUM(date)", [("DATE",)], "distributive"),
        ("1.10 + 2e5", [], None),
        ("amount /* gross */ -- a comment", [("AMOUNT",)], None),
    ],
)
def test_valid_constructs(
    text: str, references: list[tuple[str, ...]], aggregate: str | None
) -> None:
    tree = sql.parse(text)
    assert refs(text) == references
    assert sql.aggregate_class(tree) == aggregate
    assert sql.unsupported(tree) is None


def test_numbers_keep_their_written_text() -> None:
    tree = sql.parse("1.10")
    assert isinstance(tree, sql.Number)
    assert tree.text == "1.10"


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        (
            "SUM(a) OVER (PARTITION BY b ORDER BY c ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)",
            "window_function",
        ),
        ("ROW_NUMBER() OVER (ORDER BY a)", "window_function"),
        ("TO_CHAR(d, 'YYYY')", "experimental_function"),
        ("TO_DATE(s, 'YYYY-MM-DD')", "experimental_function"),
        ("FOO(a)", "unknown_function"),
    ],
)
def test_valid_trees_the_product_does_not_implement(text: str, operation: str) -> None:
    assert sql.unsupported(sql.parse(text)) == operation


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("a IN (SELECT b FROM t)", "subquery"),
        ("(SELECT 1)", "subquery"),
        ("SELECT", "subquery"),
        ("a::INTEGER", "cast_operator"),
        ("d + INTERVAL '1' DAY", "interval"),
        ("DATE '2024-01-01'", "typed_literal"),
        ("CAST(a AS GEOMETRY)", "cast_type"),
        ("a ^ 2", "operator"),
        ("items[1]", "operator"),
        ("$param", "operator"),
    ],
)
def test_recognizable_sql_outside_the_subset_is_unsupported(text: str, operation: str) -> None:
    with pytest.raises(sql.SqlUnsupported) as caught:
        sql.parse(text)
    assert caught.value.operation == operation


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   -- only a comment",
        "SUM(",
        "SUM(a))",
        "a +",
        "a AND",
        "CASE END",
        "CASE WHEN a THEN 1",
        "CAST(a INTEGER)",
        "CAST(a AS DECIMAL(x))",
        "a IS",
        "a NOT 1",
        "a BETWEEN 1",
        "EXTRACT(CENTURY FROM d)",
        "a.b.c",
        "a, b",
        "SUM(a) FILTER (a > 1)",
        "LOWER(a) FILTER (WHERE a > 1)",
        "SUM(a) WITHIN GROUP (ORDER BY a)",
        "PERCENTILE_CONT(0.5)",
        "RANK()",
        "'unclosed",
        '"' + "x" * 129 + '"',
        "(" * 100 + "1" + ")" * 100,
    ],
)
def test_text_outside_the_grammar_is_a_syntax_error(text: str) -> None:
    with pytest.raises(sql.SqlSyntaxError):
        sql.parse(text)
