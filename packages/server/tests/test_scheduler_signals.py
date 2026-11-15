"""The scheduler's stop signals are watched even when the process holds 1,024 open files or
more."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from types import FrameType

import pytest
from orvex.server.scheduler import _watch_signals


@pytest.fixture
def many_files() -> Iterator[None]:
    """Enough open descriptors that the next one is past `select`'s limit of 1,024."""
    read, write = os.pipe()
    opened = [os.dup(read) for _ in range(1100)]
    try:
        yield
    finally:
        for descriptor in (*opened, read, write):
            os.close(descriptor)


@pytest.mark.usefixtures("many_files")
def test_a_stop_signal_is_seen_with_many_open_files() -> None:
    stopped: list[int] = []

    def stop(number: int, frame: FrameType | None) -> None:
        stopped.append(number)

    restore = _watch_signals(stop)
    try:
        signal.raise_signal(signal.SIGTERM)
        time.sleep(0.5)  # the watcher thread reads the wakeup byte
    finally:
        restore()
    assert signal.SIGTERM in stopped
