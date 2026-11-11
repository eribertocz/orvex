"""#437: the audit log on both stores: every access change is recorded with it, the chain and
its signatures verify, events have one gapless order under concurrency, a failure refuses the
action without recursion, and the tables stay append-only."""

from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator
from orvex.access.audit import AuditLog
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    AUDIT_EVENT_INVALID,
    AUDIT_UNAVAILABLE,
    AccessError,
)
from orvex.access.storage import PostgresStore
from pg8000.exceptions import DatabaseError

from tests.support.access_directory import Raw, World
from tools.repository import REPOSITORY_ROOT

WRITERS, EACH = 8, 1000
CONTRACT = REPOSITORY_ROOT / "contracts/audit-event/v1/schema.json"


def kinds(world: World) -> list[str]:
    return [event["kind"] for event in world.events()[world.baseline :]]


def test_every_access_change_is_recorded_with_it(world: World) -> None:
    ana = world.user("ana")
    world.project_with_builder("p1", ana)
    world.directory.create_group(ana, "p1", "collections")
    assert kinds(world) == ["user.created", "membership.added", "group.created"]
    events = world.events()[world.baseline :]
    assert events[1]["project"] == "p1"
    assert events[1]["details"] == {"user": ana, "role": "builder"}
    contract = Draft202012Validator(json.loads(CONTRACT.read_text("utf-8")))
    assert all(not list(contract.iter_errors(event)) for event in events)
    assert world.audit.verify() == len(world.events())


def test_a_refusal_is_recorded_on_its_own(world: World) -> None:
    ana = world.user("ana")
    with pytest.raises(AccessError):
        world.directory.create_user(ana, "ben", "Ben")
    last = world.events()[-1]
    assert last["kind"] == "access.refused"
    assert last["outcome"] == "refused"
    assert last["actor"] == ana
    assert last["details"] == {"operation": "create_user", "code": ACCESS_FORBIDDEN}


def test_seq_is_gapless_and_at_never_goes_back(world: World) -> None:
    for index in range(30):
        world.user(f"user{index}")
    with world.store.transaction(read_only=True) as transaction:
        rows = transaction.rows("SELECT seq, at FROM audit_events ORDER BY seq")
    assert [seq for seq, _ in rows] == list(range(1, len(rows) + 1))
    instants = [str(at) for _, at in rows]
    assert instants == sorted(instants)


def test_r1_an_unknown_kind_or_a_planted_secret_is_refused_and_writes_nothing(
    world: World,
) -> None:
    before = len(world.events())
    for kind, details in (
        ("user.renamed", {"user": "u1"}),
        ("auth.login", {"method": "password", "password": "hunter2"}),
        ("query", {"receipt": "sha256:" + "0" * 64, "rows": [["Z1", "30.75"]]}),
        ("auth.login", {"method": 0.5}),
    ):
        with pytest.raises(AccessError) as refused:
            world.audit.record_alone(kind, world.admin, details)
        assert refused.value.code == AUDIT_EVENT_INVALID
    assert len(world.events()) == before


def test_a_failed_event_takes_its_change_with_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: a failure between the change and its event leaves neither."""

    def broken(*_: object, **__: object) -> str:
        raise AccessError(AUDIT_UNAVAILABLE, "simulated", {})

    monkeypatch.setattr(world.audit, "record", broken)
    monkeypatch.setattr(world.audit, "refusal", lambda *_, **__: None)
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(world.admin, "ana", "Ana")
    assert refused.value.code == AUDIT_UNAVAILABLE
    with world.store.transaction(read_only=True) as transaction:
        assert transaction.rows("SELECT count(*) FROM users WHERE user_name = 'ana'") == [(0,)]


def test_a_failed_log_refuses_without_recursion(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC5: the refusal's own event cannot be recorded either: one process-log line, counted,
    and no further attempt through the log."""
    ana = world.user("ana")
    attempts: list[str] = []

    def unavailable(kind: str, *_: object, **__: object) -> str:
        attempts.append(kind)
        raise AccessError(AUDIT_UNAVAILABLE, "simulated", {})

    monkeypatch.setattr(world.audit, "record_alone", unavailable)
    with caplog.at_level(logging.ERROR), pytest.raises(AccessError) as refused:
        world.directory.create_user(ana, "ben", "Ben")
    assert refused.value.code == ACCESS_FORBIDDEN
    assert attempts == ["access.refused"]
    assert world.audit.unrecorded == 1
    assert [record.getMessage() for record in caplog.records] == [
        "audit: a refusal could not be recorded"
    ]


def test_ac4_eight_writers_give_one_gapless_valid_chain(world: World) -> None:
    before = len(world.events())

    def writer(index: int) -> None:
        for number in range(EACH):
            world.audit.record_alone(
                "access.refused",
                world.admin,
                {"operation": f"writer_{index}", "code": "ACCESS_FORBIDDEN", "target": str(number)},
                outcome="refused",
            )

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(writer, range(WRITERS)))
    events = world.events()
    assert len(events) == before + WRITERS * EACH
    written = [e for e in events if e["details"].get("operation", "").startswith("writer_")]
    assert len({(e["details"]["operation"], e["details"]["target"]) for e in written}) == 8000
    assert world.audit.verify() == len(events)
    assert len(world.audit.checkpoints()) >= len(events) // 1000


def test_ac7_the_tables_refuse_update_and_delete(world: World) -> None:
    """On PostgreSQL the store's own role holds no UPDATE or DELETE grant; on SQLite the
    triggers refuse them."""
    world.user("ana")
    if isinstance(world.store, PostgresStore):
        with world.store.transaction() as transaction:
            for statement in (
                "UPDATE audit_events SET kind = 'auth.logout'",
                "DELETE FROM audit_events",
            ):
                transaction.run("SAVEPOINT attempt")
                with pytest.raises(DatabaseError, match="permission denied"):
                    transaction.run(statement)
                transaction.run("ROLLBACK TO SAVEPOINT attempt")
        return
    raw = Raw(world.backend, world)
    try:
        for statement in (
            "UPDATE audit_events SET kind = 'auth.logout'",
            "DELETE FROM audit_events",
            "UPDATE audit_checkpoints SET hash = hash",
            "DELETE FROM audit_keys",
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match=r"append-only|only by retention|never"
            ):
                raw.run(statement)
    finally:
        raw.close()


def test_a_checkpoint_is_written_every_n_events_and_daily(world: World) -> None:
    moments = iter(
        [datetime(2026, 11, 10, 12, tzinfo=UTC)] * 5 + [datetime(2026, 11, 12, 12, tzinfo=UTC)]
    )
    log = AuditLog(
        world.store,
        world.audit.signing_key,
        world.audit.anchors,
        clock=lambda: next(moments),
        checkpoint_every=4,
    )
    start = len(world.events())
    for _ in range(6):
        log.record_alone("auth.logout", world.admin, {})
    covered = {row.seq for row in log.checkpoints()}
    assert {seq for seq in range(start + 1, start + 7) if seq % 4 == 0} <= covered
    assert start + 6 in covered  # two days later: the daily checkpoint
    anchored = world.audit.anchors.read_text("ascii").splitlines()
    assert [row.anchor() for row in log.checkpoints()] == anchored
