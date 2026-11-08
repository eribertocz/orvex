"""Read an optional file with a security effect strictly (#410): absent, or a regular file.

Anything else at its path (a symbolic link, valid or broken, a directory, a FIFO, a device) is
refused, never taken as absent, so a policy file cannot be switched off by replacing it. The
check and the read use one descriptor, opened without following a link and without blocking,
so nothing can be swapped in between.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class NotRegularFile(OSError):
    """Something is at the path, but it is not a regular file."""


def read_regular_file(path: Path) -> bytes | None:
    """The file's bytes; None when nothing is at `path`. `NotRegularFile` for anything but a
    regular file; any other `OSError` (an unreadable file) as raised."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise NotRegularFile(errno.ELOOP, "a symbolic link, not a regular file") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            return file.read()
    finally:
        os.close(descriptor)
