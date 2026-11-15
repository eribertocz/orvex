"""Rotating an instance's audit signing key on disk (#437): the new key staged beside the old,
made the log's key, then moved into place.

A staged key left by an interrupted rotation is refused (`AUDIT_KEY_UNSAFE`, naming the file)
and never overwritten: move it into place if the log already uses it (verify says which), or
remove it.
"""

from __future__ import annotations

import os
from pathlib import Path

from orvex.access.audit import signing_key
from orvex.access.audit.log import SYSTEM, AuditLog
from orvex.contracts.access.errors import AUDIT_KEY_UNSAFE, AccessError


def rotate_key(log: AuditLog, key: Path, actor: str = SYSTEM) -> str:
    """Rotate the key whose file is `key`; return the new key's fingerprint."""
    staged = key.with_name(key.name + ".new")
    if staged.exists() or staged.is_symlink():
        raise AccessError(
            AUDIT_KEY_UNSAFE,
            "A staged signing key exists: a rotation was interrupted. Move it into place if the "
            "log already uses it (verify says which), or remove it.",
            {"path": str(staged)},
        )
    new = signing_key.create(staged)
    log.rotate(new, actor)
    os.replace(staged, key)
    return new.fingerprint
