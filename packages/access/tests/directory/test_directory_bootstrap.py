"""#436 item 10 and AC11: the first administrator, its recovery file, and the refusals of
`admin create` without `--recover`, or with a wrong, missing or unsafe recovery file."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from orvex.access.directory import Bootstrap, DirectoryEvent
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NAME_TAKEN, AccessError

from tests.support.access_directory import Backend


@pytest.fixture
def made(backend: Backend, tmp_path: Path) -> tuple[Bootstrap, list[DirectoryEvent], Path]:
    events: list[DirectoryEvent] = []
    recovery = tmp_path / "access-recovery.key"
    return Bootstrap(backend.store(), recovery, events.append), events, recovery


def test_the_first_run_writes_an_owner_only_recovery_file_once(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, _, recovery = made
    assert bootstrap.initialize() is True
    first = recovery.read_text("ascii")
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o600
    assert len(first.strip()) == 64
    assert bootstrap.initialize() is False
    assert recovery.read_text("ascii") == first  # never written again


def test_create_makes_an_active_administrator_and_audits_it(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, events, _ = made
    created = bootstrap.create_administrator("root", "Root")
    assert created.administrator
    assert created.active
    assert events == [DirectoryEvent("admin_create", "done", None, target=created.id)]


def test_create_is_refused_once_an_active_administrator_exists(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, events, _ = made
    bootstrap.create_administrator("root", "Root")
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("second", "Second")
    assert refused.value.code == ACCESS_FORBIDDEN
    assert events[-1] == DirectoryEvent("admin_create", "refused", None, code=ACCESS_FORBIDDEN)


def test_recover_with_the_recovery_file_creates_another_administrator(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, events, _ = made
    bootstrap.create_administrator("root", "Root")
    recovered = bootstrap.create_administrator("rescue", "Rescue", recover=True)
    assert recovered.administrator
    assert events[-1] == DirectoryEvent("admin_recover", "done", None, target=recovered.id)


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
def test_recover_without_the_right_safe_file_is_refused(
    made: tuple[Bootstrap, list[DirectoryEvent], Path], spoil: object
) -> None:
    bootstrap, events, recovery = made
    bootstrap.create_administrator("root", "Root")
    assert callable(spoil)
    spoil(recovery)
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("rescue", "Rescue", recover=True)
    assert refused.value.code == ACCESS_FORBIDDEN
    assert events[-1] == DirectoryEvent("admin_recover", "refused", None, code=ACCESS_FORBIDDEN)


def test_recover_reads_a_file_moved_elsewhere(
    made: tuple[Bootstrap, list[DirectoryEvent], Path], tmp_path: Path
) -> None:
    bootstrap, _, recovery = made
    bootstrap.create_administrator("root", "Root")
    moved = tmp_path / "offline.key"
    recovery.rename(moved)
    assert bootstrap.create_administrator("rescue", "Rescue", recover=True, recovery=moved).active


def test_create_refuses_a_taken_user_name(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, _, _ = made
    bootstrap.create_administrator("root", "Root")
    with pytest.raises(AccessError) as refused:
        bootstrap.create_administrator("ROOT", "Again", recover=True)
    assert refused.value.code == USER_NAME_TAKEN


def test_the_recovery_secret_never_reaches_an_event(
    made: tuple[Bootstrap, list[DirectoryEvent], Path],
) -> None:
    bootstrap, events, recovery = made
    bootstrap.create_administrator("root", "Root")
    bootstrap.create_administrator("rescue", "Rescue", recover=True)
    secret = recovery.read_text("ascii").strip()
    assert all(secret not in repr(event) for event in events)
    assert os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "-") not in repr(events)
