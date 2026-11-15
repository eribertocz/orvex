"""The transform kit's cases for the `schedule` and `lineage` capabilities: what is due and when,
in explicit time zones, and a run's OpenLineage events."""

from __future__ import annotations

import datetime as dt
import json

from orvex.contracts.kit import KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.transform.analysis import (
    ColumnsNeeded,
    LineageRun,
    LineageScope,
    RelationColumns,
)
from orvex.contracts.transform.kit_project import (
    COLUMNS,
    CONNECTIONS,
    LA_PAZ,
    NEW_YORK,
    at,
    expect,
    project,
)
from orvex.contracts.transform.scheduling import LOOK_AGAIN, History

SCOPE = LineageScope("urn:kit:transform:1.0", "kit://meters", "ducklake://meters")
PARENT = "5b0e6a2c-1f3d-4c8e-9a7b-6d5c4b3a2f1e"
RUN = LineageRun(
    "marts.daily",
    "9c8b7a6f-5e4d-4c3b-8a2f-1e0d9c8b7a6f",
    "2026-03-08T01:30:00.000000+00:00",
    "2026-03-08T01:31:00.000000+00:00",
    "success",
    COLUMNS,
    COLUMNS,
    "1",
    None,
)


def never_run_is_due(client: KitClient) -> None:
    models = project(client)
    graph = client.build_graph(tuple(models.values()), ("feed",), (), ())
    now = at("2026-10-28T10:00")
    plan = client.schedule_plan(
        tuple(models.values()),
        graph.order,
        graph.dependencies,
        {},
        now,
        "America/La_Paz",
        now,
        None,
        {},
    )
    expect(plan.models == ("raw.readings", "staging.active", "marts.fresh"), f"plan {plan}")
    expect(plan.reasons == ("marts.fresh: freshness 10m",), f"reasons {plan.reasons}")
    expect(plan.wake_at == now + LOOK_AGAIN and plan.wake_at.tzinfo == LA_PAZ, "zone lost")
    expect(plan.full_refresh == (), f"full refresh {plan.full_refresh}")


def fresh_is_not_due(client: KitClient) -> None:
    models = project(client)
    graph = client.build_graph(tuple(models.values()), ("feed",), (), ())
    now = at("2026-10-28T10:00")
    done = History(now - dt.timedelta(minutes=2), now - dt.timedelta(minutes=1))
    plan = client.schedule_plan(
        tuple(models.values()),
        graph.order,
        graph.dependencies,
        {"marts.fresh": done, "marts.weekly": History(last_full_refresh=now)},
        now,
        "America/La_Paz",
        now - dt.timedelta(hours=1),
        now - dt.timedelta(minutes=2),
        {},
    )
    expect(plan.models == (), f"a fresh model is due: {plan}")
    expect(plan.next_run is not None and plan.next_run > now, f"next run {plan.next_run}")


def next_due_in_zone(client: KitClient) -> None:
    weekly = project(client)["marts.weekly"]
    found = client.next_due(weekly, at("2026-10-28T00:00", NEW_YORK), "America/La_Paz")
    expect(found == at("2026-11-01T03:00", NEW_YORK), f"due {found}")
    expect(found.utcoffset() is not None, "an instant without a zone")


def closure_reads_upstream(client: KitClient) -> None:
    models = project(client)
    graph = client.build_graph(tuple(models.values()), ("feed",), (), ())
    found = client.closure("marts.fresh", graph.dependencies, tuple(models))
    expect(found == ("marts.fresh", "raw.readings", "staging.active"), f"closure {found}")
    alone = client.closure("marts.fresh", graph.dependencies, ())
    expect(alone == ("marts.fresh",), f"unknown models were followed: {alone}")


def project_key_stable(client: KitClient) -> None:
    found = client.lineage_project_key("Meter Readings")
    expect(found == "meter-readings", f"key {found}")


def lineage_events_built(client: KitClient) -> None:
    models = tuple(project(client).values())
    asked = client.lineage_events(SCOPE, PARENT, (RUN,), models, CONNECTIONS, ())
    expect(asked == ColumnsNeeded((("raw", "readings"),)), f"answered {asked!r}")
    known = (RelationColumns(("raw", "readings"), ("id", "kwh", "read_at")),)
    found = client.lineage_events(SCOPE, PARENT, (RUN,), models, CONNECTIONS, known)
    expect(not isinstance(found, ColumnsNeeded), f"answered {found!r}")
    events = [json.loads(text) for text in found]
    expect([e["eventType"] for e in events] == ["START", "COMPLETE"], f"events {events}")
    expect(all(e["job"]["name"] == "marts.daily" for e in events), "the job is the model")
    expect(events[0]["producer"] == SCOPE.producer, "the producer is the scope's")


CASES = (
    KitCase(
        "schedule-plan.never-run-due",
        "schedule_plan",
        "a model with freshness that never ran is due now, upstream first, waking in its zone",
        never_run_is_due,
    ),
    KitCase(
        "schedule-plan.fresh-not-due",
        "schedule_plan",
        "a model that just succeeded is not due; the next run is later",
        fresh_is_not_due,
    ),
    KitCase(
        "next-due.time-zone",
        "next_due",
        "a weekly full refresh falls on its cron in its own time zone, across a DST change",
        next_due_in_zone,
    ),
    KitCase(
        "closure.known-upstream",
        "closure",
        "a model's closure is itself and every known model it reads, sorted",
        closure_reads_upstream,
    ),
    KitCase(
        "lineage-project-key.slug",
        "lineage_project_key",
        "a project's lineage key is its name in lowercase kebab-case",
        project_key_stable,
    ),
    KitCase(
        "lineage-events.start-complete",
        "lineage_events",
        "a run's events ask for the columns they read, then are START and COMPLETE per model",
        lineage_events_built,
    ),
)
