"""#436 item 10 and AC11: the first administrator, its recovery file, and the refusals of
`admin create` without `--recover`, or with a wrong, missing or unsafe recovery file."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from orvex.access.audit import open_audit_log
from orvex.access.directory import Bootstrap
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NAME_TAKEN, AccessError

from tests.support.access_directory import AnyStore, Backend, audit_events


class Events:
    """The audit events the bootstrap recorded, read back from the store."""

    def __init__(self, store: AnyStore) -> None:
        self._store = store

    def last(self) -> tuple[str, str, dict[str, Any]]:
        event = audit_events(self._store)[-1]
        return event["kind"], event["outcome"], event["details"]

    def all(self) -> list[dict[str, Any]]:
        return audit_events(self._store)


type Made = tuple[Bootstrap, Events, Path]


@pytest.fixture
def made(backend: Backend, tmp_path: Path) -> Made:
    store = backend.store()
    recovery = tmp_path / "access-recovery.key"
    audit = open_audit_log(store, tmp_path / "audit-signing.key", tmp_path / "audit-anchors.log")
    return Bootstrap(store, recovery, audit), Events(store), recovery


def test_the_first_run_writes_an_owner_only_recovery_file_once(
    made: Made,
) -> None:
    bootstrap, _, recovery = made
    assert bootstrap.initialize() is True
    first = recovery.read_text("ascii")
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o600
    assert len(first.strip()) == 64
    assert bootstrap.initialize() is False
    assert recovery.read_text("ascii") == first  # never written again


def test_create_makes_an_active_administrator_and_audits_it(
    made: Made,
) -> None:
    bootstrap, events, _ = made
    created = bootstrap.create_administrator("root", "Root")
    assert created.administrator
    assert created.active
    assert events.last() == ("user.created", "allowed", {"user": created.id, "via": "admin_create"})
    assert events.all()[-1]["actor"] == "system"


def test_create_is_refused_once_an_active_administrator_exists(
    made: Made,
) -> None:
    bootstrap, events, _ = made
    bootstrap.create_administrator("root", "Root")
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("second", "Second")
    assert refused.value.code == ACCESS_FORBIDDEN
    expected = {"operation": "admin_create", "code": ACCESS_FORBIDDEN}
    assert events.last() == ("access.refused", "refused", expected)


def test_recover_with_the_recovery_file_creates_another_administrator(
    made: Made,
) -> None:
    bootstrap, events, _ = made
    bootstrap.create_administrator("root", "Root")
    recovered = bootstrap.create_administrator("rescue", "Rescue", recover=True)
    assert recovered.administrator
    via = {"user": recovered.id, "via": "admin_recover"}
    assert events.last() == ("user.created", "allowed", via)


def wrong_secret(path: Path) -> None:
    path.write_text("0" * 64 + "\n", "ascii")
    path.chmod(0o600)


def readable_by_others(path: Path) -> None:
    path.chmod(0o644)


def replaced_by_a_link(path: Path) -> None:
    kept = path.with_name("kept")
    path.rename(kept)
    path.symlink_to(kept)


def replaced_by_a_fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path, 0o600)


@pytest.mark.parametrize(
    "spoil",
    [wrong_secret, readable_by_others, replaced_by_a_link, replaced_by_a_fifo, Path.unlink],
    ids=lambda f: f.__name__,
)
def test_recover_without_the_right_safe_file_is_refused(made: Made, spoil: object) -> None:
    bootstrap, events, recovery = made
    bootstrap.create_administrator("root", "Root")
    assert callable(spoil)
    spoil(recovery)
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("rescue", "Rescue", recover=True)
    assert refused.value.code == ACCESS_FORBIDDEN
    expected = {"operation": "admin_recover", "code": ACCESS_FORBIDDEN}
    assert events.last() == ("access.refused", "refused", expected)


def test_recover_reads_a_file_moved_elsewhere(made: Made, tmp_path: Path) -> None:
    bootstrap, _, recovery = made
    bootstrap.create_administrator("root", "Root")
    moved = tmp_path / "offline.key"
    recovery.rename(moved)
    assert bootstrap.create_administrator("rescue", "Rescue", recover=True, recovery=moved).active


def test_create_refuses_a_taken_user_name(
    made: Made,
) -> None:
    bootstrap, _, _ = made
    bootstrap.create_administrator("root", "Root")
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("ROOT", "Again", recover=True)
    assert refused.value.code == USER_NAME_TAKEN


def test_the_recovery_secret_never_reaches_an_event(
    made: Made,
) -> None:
    bootstrap, events, recovery = made
    bootstrap.create_administrator("root", "Root")
    bootstrap.create_administrator("rescue", "Rescue", recover=True)
    secret = recovery.read_text("ascii").strip()
    assert all(secret not in json.dumps(event) for event in events.all())
    assert os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "-") not in json.dumps(events.all())
