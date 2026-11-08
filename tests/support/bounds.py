"""Cost bounds that hold on a shared CI runner (#329): a test proves the work an operation does,
not the seconds a busy machine takes to do it.

- `linear`: the time grows at most in proportion to the input. A quadratic regression fails
  however fast or slow the runner is, since both sizes run on the same runner moments apart.
- `FolderReads`: the folders and entries a directory walk reads, for a walk's budget.
- `SAFETY_NET`: an elapsed-time limit only to catch a hang, never a slow runner.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from typing import Protocol, Self

import pytest

SAFETY_NET = 30.0  # seconds

# Growing the input by FACTOR grows linear work by FACTOR and quadratic work by FACTOR²; the
# limit between them leaves room for noise either way.
FACTOR = 4
LIMIT = 8.0


def _best(run: Callable[[int], object], size: int) -> float:
    best = SAFETY_NET
    for _ in range(3):  # the fastest of three: noise only ever adds time
        started = time.perf_counter()
        run(size)
        best = min(best, time.perf_counter() - started)
    assert best < SAFETY_NET
    return best


# A run shorter than this is mostly fixed cost and scheduling noise: the size grows until the
# small run takes at least this long (or reaches `size * GROWTH`), so the ratio measures work.
MEASURABLE = 0.02  # seconds
GROWTH = 16


def linear(run: Callable[[int], object], size: int) -> None:
    """Fail unless `run(FACTOR * n)` takes under `LIMIT` times as long as `run(n)`, from `size`
    up to the size whose run is measurable."""
    small, n = _best(run, size), size
    while small < MEASURABLE and n < size * GROWTH:
        n *= 2
        small = _best(run, n)
    small = max(small, 1e-4)  # below the clock's useful resolution, use its floor
    large = _best(run, FACTOR * n)
    assert large / small < LIMIT, f"{n}: {small:.4f}s; {FACTOR * n}: {large:.4f}s"


class _Listing(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(self, *raised: object) -> None: ...
    def __iter__(self) -> Iterator[os.DirEntry[str]]: ...


class FolderReads:
    """The folders read through `os.scandir` and the entries gone through, while installed: the
    work a directory walk does, counted instead of timed."""

    def __init__(self) -> None:
        self.folders = 0
        self.entries = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FolderReads:
        real = os.scandir
        reads = self

        class Counted:
            def __init__(self, found: _Listing) -> None:
                self._found = found

            def __enter__(self) -> Counted:
                self._found.__enter__()
                return self

            def __exit__(self, *raised: object) -> None:
                self._found.__exit__(*raised)

            def __iter__(self) -> Iterator[os.DirEntry[str]]:
                for entry in self._found:
                    reads.entries += 1
                    yield entry

        def counted(path: str) -> Counted:
            found = real(path)  # a folder that cannot be read is not counted as read
            reads.folders += 1
            return Counted(found)

        monkeypatch.setattr(os, "scandir", counted)
        return self
