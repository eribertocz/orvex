"""The instance's recovery file: a 256-bit secret, owner-only, for `admin create --recover`
(adapter).

The store keeps only the secret's SHA-256 digest; the file keeps the secret. A file is read
only if it is a regular file (not a link), owned by the user running the command, and readable
by nobody else. A file that fails any of these is treated as absent: the recovery is refused,
never weakened.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

_SECRET = re.compile(r"[0-9a-f]{64}")


def digest_of(secret: str) -> str:
    return hashlib.sha256(bytes.fromhex(secret)).hexdigest()


def write_new(path: Path) -> str:
    """Write a new secret to `path`, replacing it atomically; return the secret's digest."""
    secret = secrets.token_hex(32)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as file:
            file.write(secret + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return digest_of(secret)


def read_digest(path: Path) -> str | None:
    """The digest of the secret in `path`, or None when the file is missing or unsafe."""
    try:
        # `O_NOFOLLOW`: a link is refused when the file is opened, and the checks below are of
        # the file actually opened, so nothing can be swapped in between.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError, OSError:
        return None
    with os.fdopen(descriptor, "rb") as file:
        status = os.fstat(file.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            return None
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            return None
        text = file.read(128).decode("ascii", errors="replace").strip()
    return digest_of(text) if _SECRET.fullmatch(text) else None
