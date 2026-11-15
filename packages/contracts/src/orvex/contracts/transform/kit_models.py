"""The transform kit's cases for the `models` and `compile` capabilities: discovery, the graph,
a model's problems, the automatic filter, the SQL a model runs, its tests and reconciliation."""

from __future__ import annotations

from orvex.contracts.kit import KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.transform.analysis import (
    ColumnsNeeded,
    MeasureDifference,
    ReconciliationStage,
    Refusal,
    RelationColumns,
)
from orvex.contracts.transform.kit_project import (
    COLUMNS,
    FILES,
    READINGS_SQL,
    discover,
    expect,
    project,
)

READINGS = RelationColumns(("raw", "readings"), ("id", "kwh", "read_at"))


def _codes(problems: object) -> list[tuple[str, str | None]]:
    return [(p.code.root, p.details.path) for p in problems]  # type: ignore[attr-defined]


def discovered_in_order(client: KitClient) -> None:
    found = discover(client)
    names = [model.name for model in found.models]
    expect(names == sorted(names) and len(names) == 5, f"models {names}")
    readings = next(model for model in found.models if model.name == "raw.readings")
    expect(readings.sql == READINGS_SQL, "the SQL is not kept as written")
    expect(readings.sql_file == "models/raw/readings.sql", f"file {readings.sql_file}")
    daily = next(model for model in found.models if model.name == "marts.daily")
    expect(daily.config.materialized == "incremental", "the sidecar was not applied")


def discovery_reports_problems(client: KitClient) -> None:
    found = discover(client, {**FILES, "models/marts/weekly.yaml": "version: 2\n"})
    codes = [problem.details.file for problem in found.problems]
    expect(codes == ["models/marts/weekly.yaml"], f"problems {found.problems}")
    expect("marts.weekly" not in {model.name for model in found.models}, "a refused model")


def graph_orders_dependencies_first(client: KitClient) -> None:
    models = tuple(project(client).values())
    graph = client.build_graph(models, ("feed",), (), ())
    expect(graph.problems == (), f"problems {graph.problems}")
    order = list(graph.order)
    expect(order.index("raw.readings") < order.index("staging.active"), f"order {order}")
    expect(order.index("staging.active") < order.index("marts.fresh"), f"order {order}")
    expect(graph.dependencies["marts.fresh"] == ("staging.active",), "dependencies")


def graph_refuses_unknown_reference(client: KitClient) -> None:
    files = {"models/s/m.sql": "SELECT * FROM nowhere.t"}
    models = discover(client, files).models
    graph = client.build_graph(models, (), (), ())
    expect([p.code.root for p in graph.problems] == ["MODEL_REFERENCE_UNKNOWN"], "no problem")


def output_names_known_from_text(client: KitClient) -> None:
    names = client.known_output_names("SELECT 1 AS a, 2 AS b")
    expect(names == ("a", "b"), f"names {names}")


def config_problems_reported(client: KitClient) -> None:
    files = {"models/s/m.sql": "SELECT 1 AS id", "models/s/m.yaml": "version: 1\nfreshness: 4m\n"}
    model = discover(client, files).models[0]
    found = client.config_problems(model, "America/La_Paz", {})
    expect(_codes(found) == [("FRESHNESS_TOO_SHORT", "/freshness")], f"problems {found}")
    clean = client.config_problems(project(client)["marts.fresh"], "UTC", {"feed": "parquet"})
    expect(clean == (), f"a valid sidecar has problems: {clean}")


def column_problems_reported(client: KitClient) -> None:
    models = project(client)
    columns = dict.fromkeys(models, COLUMNS)
    expect(client.column_problems(tuple(models.values()), columns) == (), "clean project")
    columns["marts.daily"] = (("id", "BIGINT"), ("kwh", "DECIMAL(18,4)"))
    found = client.column_problems(tuple(models.values()), columns)
    expect(len(found) == 1 and found[0].details.model == "marts.daily", f"problems {found}")


def test_problems_reported(client: KitClient) -> None:
    models = project(client)
    columns = dict.fromkeys(models, COLUMNS)
    fresh = models["marts.fresh"]
    expect(client.test_problems(fresh, columns, tuple(models.values())) == (), "clean tests")
    columns["marts.fresh"] = (("kwh", "DECIMAL(18,4)"),)
    found = client.test_problems(fresh, columns, tuple(models.values()))
    expect(len(found) == 1 and found[0].details.path == "/tests/0/not_null/0", f"problems {found}")


ALIASED = {
    "models/raw/r.sql": "SELECT 1 AS id, now() AS read_at",
    "models/s/m.sql": "SELECT id, read_at AS t FROM raw.r WHERE t > $since",
    "models/s/m.yaml": (
        "version: 1\nmaterialized: incremental\n"
        "incremental: {strategy: append, time_column: t, filter: manual}\n"
    ),
}


def since_bound_asks_for_columns(client: KitClient) -> None:
    model = next(m for m in discover(client, ALIASED).models if m.name == "s.m")
    asked = client.since_bound_problem(model, ())
    expect(asked == ColumnsNeeded((("raw", "r"),)), f"answered {asked!r}")
    known = (RelationColumns(("raw", "r"), ("ID", "Read_At")),)
    expect(client.since_bound_problem(model, known) is None, "a column bound is refused")


def since_recognized(client: KitClient) -> None:
    expect(client.uses_since("SELECT 1 WHERE x > $since") is True, "$since not seen")
    expect(client.uses_since("SELECT '$since' AS t") is False, "a string is not $since")


def filter_proven_with_columns(client: KitClient) -> None:
    sql = project(client)["marts.daily"].sql
    asked = client.filtered_sql(sql, "read_at", ())
    expect(asked == ColumnsNeeded((("raw", "readings"),)), f"answered {asked!r}")
    found = client.filtered_sql(sql, "read_at", (READINGS,))
    expect(found == f"{sql} WHERE (readings.read_at > $since)", f"filtered {found!r}")


def filter_refused_with_line(client: KitClient) -> None:
    sql = "SELECT\nmax(read_at) AS read_at FROM raw.readings"
    found = client.filtered_sql(sql, "read_at", (READINGS,))
    expect(isinstance(found, Refusal) and found.reason == "aggregation", f"answered {found!r}")
    expect(isinstance(found, Refusal) and found.line == 2, f"line of {found!r}")


def views_inlined(client: KitClient) -> None:
    models = project(client)
    found = client.compiled_sql(models["marts.fresh"], tuple(models.values()), None)
    expected = "SELECT * FROM (SELECT id, kwh, read_at FROM raw.readings WHERE (kwh > 0)) AS active"
    expect(found == expected, f"compiled {found!r}")
    given = client.compiled_sql(models["marts.weekly"], tuple(models.values()), "SELECT 2 AS x")
    expect(given == "SELECT 2 AS x", f"the SQL given is not the one compiled: {given!r}")


def tests_read_relations(client: KitClient) -> None:
    relations = client.test_relations(tuple(project(client).values()))
    expect(relations["marts.fresh"] == '"marts"."fresh"', f"relations {relations}")
    expect(relations["staging.active"].startswith("(SELECT "), "a view is not its subquery")


def tests_count_failing_rows(client: KitClient) -> None:
    models = project(client)
    relations = client.test_relations(tuple(models.values()))
    tests = client.test_steps(models["marts.fresh"], '"marts"."fresh"', COLUMNS, relations)
    expect([test.name for test in tests] == ["not_null(id)"], f"tests {tests}")
    request = tests[0].request
    expected = 'SELECT count(*) FROM "marts"."fresh" AS t WHERE "id" IS NULL'
    expect(request.sql == expected and request.parameters == [], f"request {request}")


def reconciliation_staged_for_databases(client: KitClient) -> None:
    fresh = project(client)["marts.fresh"]
    expect(client.reconciliation_stage(fresh, 0, ()) is None, "a file source is staged")
    stage = client.reconciliation_stage(fresh, 0, ("feed",))
    expect(isinstance(stage, ReconciliationStage) and stage.database == "feed", f"{stage!r}")
    staged = stage is not None and stage.schema == "_stage" and '"feed"."data"' in stage.source_sql
    expect(staged, f"the stage does not read the source's side: {stage!r}")


def measures_compared(client: KitClient) -> None:
    fresh = project(client)["marts.fresh"]
    sql = client.measures_sql(fresh, 0, '"marts"."fresh"', None)
    expect('FROM "marts"."fresh"' in sql and 'FROM "feed"."data"' in sql, f"sql {sql}")
    staged = client.measures_sql(fresh, 0, '"marts"."fresh"', '"_stage"."x"')
    expect('"_stage"."x"' in staged and '"feed"."data"' not in staged, f"staged {staged}")


def measure_types_exact(client: KitClient) -> None:
    exact = client.measure_difference("DECIMAL(18,4)", "BIGINT")
    expect(exact == MeasureDifference("DECIMAL(38,4)", ()), f"exact {exact!r}")
    floats = client.measure_difference("DOUBLE", "BIGINT")
    expect(floats.inexact == ("DOUBLE",), f"a float compared: {floats!r}")


CASES = (
    KitCase(
        "discover-models.sorted-exact",
        "discover_models",
        "models come back sorted by name, with their SQL as written and their sidecar applied",
        discovered_in_order,
    ),
    KitCase(
        "discover-models.problems",
        "discover_models",
        "a refused sidecar is a problem naming its file, and its model is not discovered",
        discovery_reports_problems,
    ),
    KitCase(
        "build-graph.order",
        "build_graph",
        "models are ordered with their dependencies first",
        graph_orders_dependencies_first,
    ),
    KitCase(
        "build-graph.unknown-reference",
        "build_graph",
        "a reference to no model or source is MODEL_REFERENCE_UNKNOWN",
        graph_refuses_unknown_reference,
    ),
    KitCase(
        "known-output-names.from-text",
        "known_output_names",
        "the output names a SELECT list decides are known from its text",
        output_names_known_from_text,
    ),
    KitCase(
        "config-problems.freshness",
        "config_problems",
        "a freshness under the minimum is FRESHNESS_TOO_SHORT at /freshness; a valid one is clean",
        config_problems_reported,
    ),
    KitCase(
        "column-problems.time-column",
        "column_problems",
        "an incremental model whose output lacks its time column has a problem",
        column_problems_reported,
    ),
    KitCase(
        "test-problems.unknown-column",
        "test_problems",
        "a test of a column the model does not output is a problem at its pointer",
        test_problems_reported,
    ),
    KitCase(
        "since-bound-problem.columns-needed",
        "since_bound_problem",
        "a bound on an alias asks for the columns it reads, then answers with them",
        since_bound_asks_for_columns,
    ),
    KitCase(
        "uses-since.parameter",
        "uses_since",
        "$since is recognized as a parameter, never inside a string",
        since_recognized,
    ),
    KitCase(
        "filtered-sql.proven",
        "filtered_sql",
        "the automatic filter asks for the columns it reads, then places `> $since` exactly",
        filter_proven_with_columns,
    ),
    KitCase(
        "filtered-sql.refused",
        "filtered_sql",
        "an aggregated time column refuses the filter, with the reason and its line",
        filter_refused_with_line,
    ),
    KitCase(
        "compiled-sql.views-inlined",
        "compiled_sql",
        "a view is inlined as a subquery; the SQL given is the one compiled",
        views_inlined,
    ),
    KitCase(
        "test-relations.quoted",
        "test_relations",
        "a stored model is its quoted table, a view its subquery",
        tests_read_relations,
    ),
    KitCase(
        "test-steps.count-failing",
        "test_steps",
        "each test is one count of failing rows on the model's relation",
        tests_count_failing_rows,
    ),
    KitCase(
        "reconciliation-stage.databases",
        "reconciliation_stage",
        "only a database source's side is staged, in _stage, from the source's own SQL",
        reconciliation_staged_for_databases,
    ),
    KitCase(
        "measures-sql.both-sides",
        "measures_sql",
        "the measures read the model and the source, or the staged table in its place",
        measures_compared,
    ),
    KitCase(
        "measure-difference.exact",
        "measure_difference",
        "exact measures compare in an exact type; a float measure is named, never compared",
        measure_types_exact,
    ),
)
