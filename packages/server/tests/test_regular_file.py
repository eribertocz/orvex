"""#410: an optional file with a security effect is read only when it is a regular file, checked
and read through one descriptor; anything else at its path is an error, never "absent"."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import pytest
from orvex.server import regular_file
from orvex.server.regular_file import NotRegularFile, read_regular_file


def test_an_absent_file_is_none(tmp_path: Path) -> None:
    assert read_regular_file(tmp_path / "security.yaml") is None
    assert read_regular_file(tmp_path / "missing" / "security.yaml") is None


def test_a_regular_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "security.yaml"
    path.write_bytes(b"rules: []\n")
    assert read_regular_file(path) == b"rules: []\n"


def test_a_symbolic_link_is_refused_valid_or_broken(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_bytes(b"rules: []\n")
    valid, broken = tmp_path / "valid.yaml", tmp_path / "broken.yaml"
    valid.symlink_to(target)
    broken.symlink_to(tmp_path / "missing.yaml")
    for path in (valid, broken):
        with pytest.raises(NotRegularFile):
            read_regular_file(path)


def test_a_directory_or_a_device_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "security.yaml"
    folder.mkdir()
    for path in (folder, Path("/dev/null")):
        with pytest.raises(NotRegularFile):
            read_regular_file(path)


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "security.yaml"
    os.mkfifo(path)
    outcome: list[BaseException] = []

    def attempt() -> None:
        try:
            read_regular_file(path)
        except NotRegularFile as error:
            outcome.append(error)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(outcome) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "security.yaml"
    path.write_bytes(b"rules: []\n")
    path.chmod(0)
    with pytest.raises(PermissionError):
        read_regular_file(path)


def test_a_swap_after_the_check_does_not_change_what_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check and the read use one descriptor: replacing the path by a link right after the
    check still reads the file that was checked, and no path is examined a second time."""
    path = tmp_path / "security.yaml"
    path.write_bytes(b"checked\n")
    other = tmp_path / "other.yaml"
    other.write_bytes(b"swapped\n")
    real_fstat, real_stat, real_lstat = os.fstat, os.stat, os.lstat
    swapped: list[bool] = []

    def from_reader() -> bool:
        # Only calls made while the module under test is on the stack are trapped, however
        # they reach `os` (`pathlib` included): the runner itself (coverage, pytest's
        # reporting) may stat paths while the test runs (#429).
        frame: FrameType | None = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_filename == regular_file.__file__:
                return True
            frame = frame.f_back
        return False

    def fstat_then_swap(descriptor: int) -> os.stat_result:
        found = real_fstat(descriptor)
        if from_reader() and not swapped:
            swapped.append(True)
            path.unlink()
            path.symlink_to(other)
        return found

    def trap(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def no_path_stat(*arguments: object, **keywords: object) -> os.stat_result:
            if from_reader():
                raise AssertionError("a path was examined instead of the descriptor")
            return real(*arguments, **keywords)

        return no_path_stat

    monkeypatch.setattr(os, "fstat", fstat_then_swap)
    monkeypatch.setattr(os, "stat", trap(real_stat))
    monkeypatch.setattr(os, "lstat", trap(real_lstat))
    assert read_regular_file(path) == b"checked\n"
