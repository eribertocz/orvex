"""Ctrl-C during a load is watched even when the process holds 1,024 open files or more."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator

import pytest
from orvex.store.ctrl_c import _on_ctrl_c


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
def test_ctrl_c_calls_back_once_with_many_open_files() -> None:
    called: list[bool] = []
    pressed: list[bool] = []
    restore = _on_ctrl_c(lambda: called.append(True), pressed)
    try:
        signal.raise_signal(signal.SIGINT)
        time.sleep(0.5)  # the watcher thread reads the wakeup byte
    finally:
        restore()
    assert called == [True]
    assert pressed  # noted by the handler and by the watcher
