"""Shared test data that cannot change under a test (#376, #373 §5, Owner precision 2).

A fixture shared by several tests (`scope="module"` or wider) holds files and immutable values
only. `frozen` makes its files read-only at the operating-system level, so a test that writes to
them fails at once instead of changing what the next test reads. On Windows, `chmod` without
write permission sets the read-only attribute. A test that needs to write works on its own
`private_copy`, in its own temporary folder. Connections, lakes and other mutable state are never
shared: each test opens its own.

`frozen` is a generator for a fixture to `yield from`: it gives write permission back at teardown
so pytest can remove the folder later, on every platform.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

_READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_READ_ONLY_FOLDER = _READ_ONLY_FILE | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def _walk(folder: Path) -> Iterator[Path]:
    """Every file and folder under `folder`, the deepest first, then `folder` itself."""
    for parent, folders, files in os.walk(folder, topdown=False):
        for name in [*files, *folders]:
            yield Path(parent, name)
    yield folder


def freeze(folder: Path) -> None:
    for path in _walk(folder):
        path.chmod(_READ_ONLY_FOLDER if path.is_dir() else _READ_ONLY_FILE)


def thaw(folder: Path) -> None:
    for path in reversed(list(_walk(folder))):  # a folder first, so its entries can be changed
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def frozen(folder: Path) -> Iterator[Path]:
    """Yield `folder` read-only until the fixture ends."""
    freeze(folder)
    try:
        yield folder
    finally:
        thaw(folder)


def private_copy(template: Path, target: Path) -> Path:
    """A writable copy of a frozen `template` at `target`, for one test's own writes."""
    shutil.copytree(template, target)
    thaw(target)
    return target
