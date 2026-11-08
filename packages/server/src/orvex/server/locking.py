"""The project's exclusive lock: one process at a time holds its local lake."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

LOCK_FILE = "lake.lock"


class ProjectLocked(Exception):
    """Another process holds the project's local lake."""


@contextlib.contextmanager
def project_lock(folder: Path, name: str = LOCK_FILE) -> Iterator[None]:
    """An exclusive OS lock on `<folder>/<name>` (`lake.lock` by default), released on exit and
    when the process dies."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / name).open("a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProjectLocked from None
        yield
    finally:
        handle.close()  # closing releases the lock
