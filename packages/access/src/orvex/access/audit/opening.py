"""Opening an instance's audit log: the store migrated, and the signing key created the first
time only (application).

A missing key file is created only while the log knows no key at all, which is the instance's
initialization. Once the log knows a key, a missing or replaced file is refused
(`AUDIT_KEY_UNSAFE`), never silently replaced: a new key would let anyone who deleted the file
sign a rewritten log.
"""

from __future__ import annotations

from pathlib import Path

from orvex.access.audit import signing_key
from orvex.access.audit.log import AuditLog
from orvex.access.errors import AUDIT_KEY_UNSAFE, AccessError
from orvex.access.storage.migrations import migrate
from orvex.access.storage.store import Store


def open_audit_log(store: Store, key: Path, anchors: Path) -> AuditLog:
    migrate(store)
    if not key.exists() and not key.is_symlink():
        with store.transaction(read_only=True) as transaction:
            known = transaction.rows("SELECT count(*) FROM audit_keys")
        if known != [(0,)]:
            raise AccessError(
                AUDIT_KEY_UNSAFE, "The audit signing key's file is missing.", {"path": str(key)}
            )
        signing_key.create(key)
    return AuditLog(store, signing_key.load(key), anchors)
