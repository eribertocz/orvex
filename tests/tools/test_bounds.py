"""The cost bounds (#329): a quadratic regression fails them, a slow runner does not."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.support import bounds


def test_linear_work_passes_and_quadratic_work_fails() -> None:
    def linear_work(size: int) -> None:
        sum(range(size * 2000))

    def quadratic_work(size: int) -> None:
        sum(index for index in range(size) for _ in range(size))

    bounds.linear(linear_work, 200)
    with pytest.raises(AssertionError):
        bounds.linear(quadratic_work, 300)


def test_a_uniformly_slow_runner_still_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def slowed(size: int) -> None:
        time.sleep(0.01)  # a constant cost: a busy machine, not more work
        sum(range(size * 1000))

    bounds.linear(slowed, 100)


def test_folder_reads_count_folders_read_and_their_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        (tmp_path / f"f{index}").write_text("", "utf-8")
    reads = bounds.FolderReads().install(monkeypatch)
    with os.scandir(tmp_path) as found:
        assert len(list(found)) == 3
    with pytest.raises(FileNotFoundError):
        os.scandir(tmp_path / "missing")
    assert (reads.folders, reads.entries) == (1, 3)


def test_the_garbage_collector_never_runs_inside_a_timed_run() -> None:
    # Its cost follows the whole process's heap (other tests' objects), not the input (#403).
    import gc

    seen: list[bool] = []
    bounds.linear(lambda size: seen.append(gc.isenabled()), 1000)
    assert seen
    assert not any(seen)
    assert gc.isenabled()
