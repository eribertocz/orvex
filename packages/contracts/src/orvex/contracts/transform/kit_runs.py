"""The transform kit's cases for the `run` and `records` capabilities: the run plan, source
attachments, watermarks and `$since`, leftover tables, and the run records' statements and
requests."""

from __future__ import annotations

from orvex.contracts.kit import KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import FileSourceAttachment, StatementRequest
from orvex.contracts.transform.kit_project import COLUMNS, FEED, expect, project
from orvex.contracts.transform.steps import Comparison, Count, ModelTest, Reconcile, Watermark

RECORDS = (("_meta", "run_records_v2"),)
RUN_ID = "0b6f7c1e-8d1a-4f5e-9b2c-3a4d5e6f7a8b"
# More digits than any float or BIGINT holds: a count must stay exact text (law 11).
HUGE = "12345678901234567890123456789012345678"
TIME_ZONE = "TIMESTAMP WITH TIME ZONE"


def _plan(client: KitClient) -> dict[str, tuple[object, ...]]:
    models = project(client)
    ordered = tuple(models.values())
    graph = client.build_graph(ordered, ("feed",), (), ())
    planned = client.plan_run(
        ordered,
        graph,
        dict.fromkeys(models, COLUMNS),
        {"marts.daily": None},
        {"marts.daily": "-infinity"},
        (),
        {"marts.daily": None},
        {"marts.fresh": (Comparison("DECIMAL(38,4)"),)},
        {},
        (),
    )
    return {model.name: steps for model, steps in planned}


def run_planned(client: KitClient) -> None:
    plan = _plan(client)
    expect(list(plan) == ["raw.readings", "marts.daily", "marts.weekly", "marts.fresh"], f"{plan}")
    daily = plan["marts.daily"]
    kinds = {type(step) for step in daily}
    expect({Count, Watermark} <= kinds, f"the incremental model's steps {daily}")
    marks = [step for step in daily if isinstance(step, Watermark)]
    expect(marks[0].model == "marts.daily", f"watermark {marks[0]!r}")
    fresh = [type(step) for step in plan["marts.fresh"]]
    expect(fresh[-2:] == [ModelTest, Reconcile], f"tests then reconciliation: {fresh}")
    expect(isinstance(plan["raw.readings"][0], StatementRequest), "a table is written first")


def attachments_from_connections(client: KitClient) -> None:
    found = client.source_attachments({"feed": FEED})
    expected = FileSourceAttachment(
        kind="source_attachment", name="feed", type="parquet", path="/data/feed"
    )
    expect(found == (expected,), f"attachments {found}")


def watermark_read_at_snapshot(client: KitClient) -> None:
    daily = project(client)["marts.daily"]
    request = client.watermark_request(7, daily)
    expect(request.snapshot == 7 and "_meta.watermarks" in request.sql, f"{request}")
    expect([p.value for p in request.parameters] == ["marts.daily"], "the model is bound")


def table_watermark_read_at_snapshot(client: KitClient) -> None:
    daily = project(client)["marts.daily"]
    request = client.table_watermark_request(9, daily, TIME_ZONE)
    expect(request.snapshot == 9 and 'max("read_at")' in request.sql, f"{request}")
    expect("+00:00" in request.sql and request.parameters == [], "the watermark is UTC text")


def since_from_watermark(client: KitClient) -> None:
    expect(client.since_value(TIME_ZONE, None, None) == "-infinity", "no watermark loads all")
    found = client.since_value(TIME_ZONE, "2026-03-08T01:30:00.000000+00:00", "1h")
    expect(found == "2026-03-08T00:30:00.000000+00:00", f"since {found}")


def watermark_stored(client: KitClient) -> None:
    daily = next(s for s in _plan(client)["marts.daily"] if isinstance(s, Watermark))
    value = "2026-03-08T01:30:00.123456+00:00"
    found = client.watermark_statements(daily, value)
    inserted = [s for s in found if s.sql.startswith("INSERT INTO _meta.watermarks")]
    expect(len(inserted) == 1, f"statements {found}")
    expect([p.value for p in inserted[0].parameters] == ["marts.daily", value], "exact text")
    kept = client.watermark_statements(daily, None)
    expect(not any(s.sql.startswith("INSERT") for s in kept), "a NULL watermark is not stored")


def leftovers_found(client: KitClient) -> None:
    models = tuple(project(client).values())
    tables = (("marts", "old"), ("_meta", "watermarks"), ("marts", "fresh"), ("staging", "active"))
    found = client.leftovers(tables, models)
    expect(found == (("marts", "old"), ("staging", "active")), f"leftovers {found}")


def leftovers_dropped(client: KitClient) -> None:
    expect(client.leftover_statements(()) == (), "nothing to drop")
    found = client.leftover_statements((("marts", "old"),))
    expect(found[0].sql == 'DROP TABLE "marts"."old"', f"statements {found}")
    forget = found[-1]
    expect(forget.sql == "DELETE FROM _meta.watermarks WHERE model = $p_model", forget.sql)
    expect([p.value for p in forget.parameters] == ["marts.old"], "the name is bound")


def _record() -> RunRecord:
    return RunRecord.model_validate(
        {
            "run_id": RUN_ID,
            "model": "marts.daily",
            "trigger": "manual",
            "materialized": "incremental",
            "project_commit": None,
            "project_dirty": None,
            "versions": {"duckdb": "2.0.0", "modules": [{"name": "transform", "version": "1"}]},
            "since": "-infinity",
            "rows_read": HUGE,
            "rows_written": HUGE,
            "watermark": None,
            "tests": [],
            "reconciliation": [],
            "snapshot_id": None,
            "started_at": "2026-03-08T01:30:00.000000+00:00",
            "finished_at": "2026-03-08T01:31:00.000000+00:00",
            "outcome": "success",
            "error": None,
        }
    )


def records_written(client: KitClient) -> None:
    empty = client.record_statements(())
    expect(all(s.sql.startswith("CREATE ") for s in empty), f"no record is written: {empty}")
    found = client.record_statements((_record(),))
    values = [p.value for statement in found for p in statement.parameters]
    expect(RUN_ID in values and HUGE in values, f"the record is not bound exactly: {found}")


def history_requested(client: KitClient) -> None:
    expect(client.history_request(7, "America/La_Paz", 5, ()) is None, "no record table")
    request = client.history_request(7, "America/La_Paz", 5, RECORDS)
    expect(request is not None and request.snapshot == 7, f"request {request}")


def model_history_requested(client: KitClient) -> None:
    expect(client.model_history_request(7, ()) is None, "no record table")
    request = client.model_history_request(7, RECORDS)
    expect(request is not None and request.snapshot == 7, f"request {request}")


def last_run_requested(client: KitClient) -> None:
    expect(client.last_run_request(7, ()) is None, "no record table")
    request = client.last_run_request(7, RECORDS)
    expect(request is not None and request.snapshot == 7, f"request {request}")


def committed_requested(client: KitClient) -> None:
    request = client.committed_request(11, RUN_ID)
    expect(request.snapshot == 11, f"request {request}")
    expect(RUN_ID in [p.value for p in request.parameters], "the run is bound, not formatted")


CASES = (
    KitCase(
        "plan-run.steps",
        "plan_run",
        "stored models in run order, an incremental one counted and watermarked, tests then "
        "reconciliation last",
        run_planned,
    ),
    KitCase(
        "source-attachments.parquet",
        "source_attachments",
        "a file connection becomes the store's file attachment",
        attachments_from_connections,
    ),
    KitCase(
        "watermark-request.snapshot",
        "watermark_request",
        "the stored watermark is read at the snapshot given, the model bound",
        watermark_read_at_snapshot,
    ),
    KitCase(
        "table-watermark-request.utc",
        "table_watermark_request",
        "a table's largest time column is read at the snapshot given, as UTC text",
        table_watermark_read_at_snapshot,
    ),
    KitCase(
        "since-value.lookback",
        "since_value",
        "no watermark is -infinity; a lookback moves it back exactly, zone kept",
        since_from_watermark,
    ),
    KitCase(
        "watermark-statements.exact",
        "watermark_statements",
        "a watermark is stored as its exact text; a NULL one is not stored",
        watermark_stored,
    ),
    KitCase(
        "leftovers.model-names-only",
        "leftovers",
        "tables no model stores are leftovers, never _meta or a stored model's",
        leftovers_found,
    ),
    KitCase(
        "leftover-statements.drop-and-forget",
        "leftover_statements",
        "a leftover is dropped and its watermark forgotten, its name bound",
        leftovers_dropped,
    ),
    KitCase(
        "record-statements.exact",
        "record_statements",
        "the record tables exist; a run record is written with its values bound exactly (38-digit "
        "counts)",
        records_written,
    ),
    KitCase(
        "history-request.tables",
        "history_request",
        "no record table, no request; otherwise read at the snapshot given",
        history_requested,
    ),
    KitCase(
        "model-history-request.tables",
        "model_history_request",
        "no record table, no request; otherwise read at the snapshot given",
        model_history_requested,
    ),
    KitCase(
        "last-run-request.tables",
        "last_run_request",
        "no record table, no request; otherwise read at the snapshot given",
        last_run_requested,
    ),
    KitCase(
        "committed-request.bound",
        "committed_request",
        "the run's commit is read at the snapshot given, its id bound",
        committed_requested,
    ),
)
