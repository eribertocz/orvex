"""P1-09: dependency graph, run order and the run plan."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import StatementRequest
from orvex.transform.graph import build_graph, compiled_sql, known_output_names, plan_run
from orvex.transform.models import Discovery, discover_models


def project(folder: Path, files: dict[str, str]) -> Discovery:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    found = discover_models(folder, {})
    assert found.problems == ()
    return found


FILES = {
    "models/staging/readings.sql": "SELECT reading_id, kwh FROM archive.data",
    "models/staging/consumers.sql": "SELECT * FROM tariffs.data",
    "models/staging/active.sql": "SELECT * FROM staging.readings WHERE kwh > 0",
    "models/staging/active.yaml": "version: 1\nmaterialized: view\n",
    "models/marts/consumption.sql": (
        "WITH totals AS (SELECT sum(kwh) AS kwh FROM staging.active)\n"
        "SELECT t.kwh, c.tariff FROM totals AS t, staging.consumers AS c"
    ),
}


def test_order_is_topological_and_deterministic(tmp_path: Path) -> None:
    found = project(tmp_path, FILES)
    graph = build_graph(found.models, {"archive", "tariffs"})
    assert graph.problems == ()
    assert graph.order == (
        "staging.consumers",
        "staging.readings",
        "staging.active",
        "marts.consumption",
    )
    assert graph.dependencies["marts.consumption"] == ("staging.active", "staging.consumers")
    assert graph.sources["staging.readings"] == ("archive",)
    first = [[str(s) for s in statements] for _, statements in plan_run(found.models, graph)]
    second = [[str(s) for s in statements] for _, statements in plan_run(found.models, graph)]
    assert first == second


def test_view_models_are_inlined_not_stored(tmp_path: Path) -> None:
    found = project(tmp_path, FILES)
    graph = build_graph(found.models, {"archive", "tariffs"})
    plan = plan_run(found.models, graph)
    assert [model.name for model, _ in plan] == [
        "staging.consumers",
        "staging.readings",
        "marts.consumption",
    ]
    consumption = {model.name: statements for model, statements in plan}["marts.consumption"]
    step = consumption[1]
    assert isinstance(step, StatementRequest)
    sql = step.sql
    assert "staging.active" not in sql
    assert "staging.readings" in sql
    # The compiled SQL still parses as one CREATE TABLE … AS SELECT.
    (statement,) = duckdb.extract_statements(sql)
    assert statement.type == duckdb.StatementType.CREATE


def test_compiled_sql_returns_the_same_rows(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/s/base.sql": "SELECT range AS n FROM range(10)",
            "models/s/even.sql": "SELECT n FROM s.base WHERE n % 2 = 0",
            "models/s/even.yaml": "version: 1\nmaterialized: view\n",
            "models/s/top.sql": "SELECT max(n) AS m FROM s.even",
        },
    )
    by_name = {model.name: model for model in found.models}
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA s")
    connection.execute("CREATE TABLE s.base AS SELECT range AS n FROM range(10)")
    expected = connection.execute(
        "SELECT max(n) AS m FROM (SELECT n FROM s.base WHERE n % 2 = 0)"
    ).fetchall()
    assert connection.execute(compiled_sql(by_name["s.top"], by_name)).fetchall() == expected


def test_cycle_is_reported_in_order(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/c/a.sql": "SELECT * FROM c.b",
            "models/c/b.sql": "SELECT * FROM c.c",
            "models/c/c.sql": "SELECT * FROM c.a",
            "models/c/free.sql": "SELECT 1 AS x",
        },
    )
    graph = build_graph(found.models, set())
    (item,) = graph.problems
    assert item.code.root == "DEPENDENCY_CYCLE"
    assert "c.a → c.b → c.c → c.a" in item.message


def test_unknown_reference_has_file_and_line(tmp_path: Path) -> None:
    found = project(tmp_path, {"models/s/m.sql": "SELECT *\nFROM\n  nowhere.t"})
    graph = build_graph(found.models, {"archive"})
    (item,) = graph.problems
    assert item.code.root == "MODEL_REFERENCE_UNKNOWN"
    assert item.details.file == "models/s/m.sql"
    assert item.details.line == 3


def test_unqualified_table_is_unknown_but_cte_is_not(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/s/a.sql": "WITH x AS (SELECT 1 AS k) SELECT * FROM x",
            "models/s/b.sql": "SELECT * FROM readings",
        },
    )
    graph = build_graph(found.models, set())
    assert [(p.code.root, p.details.file) for p in graph.problems] == [
        ("MODEL_REFERENCE_UNKNOWN", "models/s/b.sql")
    ]


def test_lake_qualified_and_source_three_part_names(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/s/a.sql": "SELECT 1 AS x",
            "models/s/b.sql": "SELECT * FROM lake.s.a, legacy.billing.invoices",
        },
    )
    graph = build_graph(found.models, {"legacy"})
    assert graph.problems == ()
    assert graph.dependencies["s.b"] == ("s.a",)
    assert graph.sources["s.b"] == ("legacy",)


def test_a_model_reading_itself_is_a_cycle(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/s/a.sql": "SELECT x + 1 AS x FROM s.a",
            "models/s/v.sql": "SELECT * FROM s.v",
            "models/s/v.yaml": "version: 1\nmaterialized: view\n",
        },
    )
    graph = build_graph(found.models, set())
    assert [p.code.root for p in graph.problems] == ["DEPENDENCY_CYCLE", "DEPENDENCY_CYCLE"]
    assert "s.a → s.a" in graph.problems[0].message
    assert "s.v → s.v" in graph.problems[1].message


def test_every_independent_cycle_is_reported(tmp_path: Path) -> None:
    found = project(
        tmp_path,
        {
            "models/c/a.sql": "SELECT * FROM c.b",
            "models/c/b.sql": "SELECT * FROM c.a",
            "models/d/a.sql": "SELECT * FROM d.b",
            "models/d/b.sql": "SELECT * FROM d.a",
            "models/e/waits.sql": "SELECT * FROM c.a",
        },
    )
    graph = build_graph(found.models, set())
    assert [p.message.split(": ", 1)[1] for p in graph.problems] == [
        "c.a → c.b → c.a.",
        "d.a → d.b → d.a.",
    ]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 AS a, 2 AS A", ["a", "A"]),
        ("SELECT a, t.b FROM t", ["a", "b"]),
        ("SELECT * FROM t", []),
        ("SELECT * EXCLUDE (a) FROM t", []),
        ("SELECT * FROM (SELECT 1 AS a, 2 AS a) AS s", ["a", "a"]),
        ("WITH c AS (SELECT 1 AS a, 2 AS A) SELECT * FROM c", ["a", "A"]),
        ("SELECT 1 AS a, 1 AS A UNION ALL SELECT 2 AS b, 3 AS c", ["a", "A"]),
        ("SELECT a, x + 1 FROM t", None),
        ("SELECT * FROM a JOIN b USING (k)", None),
        ("SELECT *, 1 AS id FROM t", None),
        ("SELECT * RENAME (a AS b) FROM t", None),
        ("SELECT COLUMNS('a.*') FROM t", None),
        ("SELECT * FROM (SELECT 1, 2) AS s(a, b)", None),
        ("SELEC", None),
    ],
)
def test_known_output_names(sql: str, expected: list[str] | None) -> None:
    # #113: the names a model's text alone decides; None when only binding can tell.
    assert known_output_names(sql) == expected
