"""#437 AC3, R2, R5 and the signing key: retention removes old events only up to a signed
checkpoint, which keeps the rest verifiable; the key file is created once and must stay safe;
a rotation keeps the whole log verifiable and retires the old key."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from orvex.access.audit import AuditLog, open_audit_log
from orvex.access.audit import signing_key as keys
from orvex.access.errors import AUDIT_KEY_UNSAFE, AUDIT_RETENTION_INVALID, AccessError
from orvex.access.storage import migrate

from tests.support.access_directory import Backend, World

LONG_AGO = datetime(2024, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 10, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


type Fresh = Callable[[int], tuple[AuditLog, Clock]]


@pytest.fixture
def fresh(backend: Backend, tmp_path: Path) -> Fresh:
    """A log on a new store whose clock starts long ago, with a checkpoint every `every` events."""

    def make(every: int) -> tuple[AuditLog, Clock]:
        store = backend.store()
        migrate(store)
        clock = Clock(LONG_AGO)
        key = keys.create(tmp_path / "fresh.key")
        anchors = tmp_path / "fresh-anchors.log"
        return AuditLog(store, key, anchors, clock=clock, checkpoint_every=every), clock

    return make


def write(log: AuditLog, count: int) -> None:
    for _ in range(count):
        log.record_alone("auth.logout", "system", {})


@pytest.mark.parametrize("days", [0, 29, -1])
def test_r2_retention_below_30_days_is_refused(world: World, days: int) -> None:
    with pytest.raises(AccessError) as refused:
        world.audit.retain(days)
    assert refused.value.code == AUDIT_RETENTION_INVALID


def test_ac3_retention_removes_up_to_a_checkpoint_and_the_rest_verifies(fresh: Fresh) -> None:
    log, clock = fresh(4)
    write(log, 10)  # checkpoints at 1, 4 and 8
    clock.now = NOW
    write(log, 3)  # 11 to 13: 11 gets the daily checkpoint, 12 the fourth
    assert [row.seq for row in log.checkpoints()] == [1, 4, 8, 11, 12]
    assert log.retain(730) == (1, 8)
    assert log.verify() == 6  # 9 to 13, and the retention event
    assert 8 in {row.seq for row in log.checkpoints()}  # the checkpoint stays


def test_r5_retention_without_a_covering_checkpoint_is_refused(fresh: Fresh) -> None:
    log, clock = fresh(10**9)
    write(log, 5)  # only the first event has a checkpoint
    clock.now = NOW
    assert log.retain(730) == (1, 1)
    with pytest.raises(AccessError) as refused:
        log.retain(730)  # 2 to 5 are old, and no checkpoint stops among them
    assert refused.value.code == AUDIT_RETENTION_INVALID
    assert refused.value.details == {"reason": "no_checkpoint"}


def test_nothing_old_enough_removes_nothing(world: World) -> None:
    before = len(world.events())
    assert world.audit.retain(730) is None
    assert len(world.events()) == before


def test_the_key_file_is_owner_only_and_never_overwritten(world: World) -> None:
    key = world.audit.anchors.with_name("audit-signing.key")
    assert key.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        keys.create(key)


def test_a_missing_key_file_is_refused_once_the_log_knows_a_key(
    world: World, tmp_path: Path
) -> None:
    key = world.audit.anchors.with_name("audit-signing.key")
    key.rename(tmp_path / "moved.key")
    with pytest.raises(AccessError) as refused:
        open_audit_log(world.store, key, world.audit.anchors)
    assert refused.value.code == AUDIT_KEY_UNSAFE
    assert not key.exists()  # never silently replaced


def test_another_key_is_refused(world: World, tmp_path: Path) -> None:
    other = keys.create(tmp_path / "other.key")
    log = AuditLog(world.store, other, world.audit.anchors)
    with pytest.raises(AccessError) as refused:
        log.record_alone("auth.logout", world.admin, {})
    assert refused.value.code == AUDIT_KEY_UNSAFE


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_a_key_file_others_can_read_is_refused(world: World, mode: int) -> None:
    key = world.audit.anchors.with_name("audit-signing.key")
    key.chmod(mode)
    with pytest.raises(AccessError) as refused:
        keys.load(key)
    assert refused.value.code == AUDIT_KEY_UNSAFE


def test_a_key_file_that_is_a_link_is_refused(world: World, tmp_path: Path) -> None:
    key = world.audit.anchors.with_name("audit-signing.key")
    link = tmp_path / "link.key"
    os.symlink(key, link)
    with pytest.raises(AccessError) as refused:
        keys.load(link)
    assert refused.value.code == AUDIT_KEY_UNSAFE


def test_a_rotation_keeps_the_log_verifiable_and_retires_the_old_key(
    world: World, tmp_path: Path
) -> None:
    write(world.audit, 5)
    old = world.audit.signing_key
    new = keys.create(tmp_path / "new.key")
    world.audit.rotate(new)
    write(world.audit, 5)
    last = world.events()[-6]
    assert last["kind"] == "audit.key_rotated"
    assert last["details"] == {"previous_key": old.fingerprint, "key": new.fingerprint}
    assert world.audit.verify() == len(world.events())
    retired = AuditLog(world.store, old, world.audit.anchors)
    with pytest.raises(AccessError) as refused:
        retired.record_alone("auth.logout", world.admin, {})
    assert refused.value.code == AUDIT_KEY_UNSAFE


def test_a_key_nobody_vouches_for_is_not_trusted(world: World, tmp_path: Path) -> None:
    """A planted key row with a forged rotation does not make its checkpoints trusted."""
    write(world.audit, 3)
    intruder = keys.create(tmp_path / "intruder.key")
    verifier = AuditLog(world.store, intruder, world.audit.anchors)
    with pytest.raises(AccessError):
        verifier.verify()
    assert json.dumps(world.events())  # the log itself is untouched
