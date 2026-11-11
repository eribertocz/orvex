"""Each user's stored password (application, #438): one row per user, the hash only.

A temporary password, set by an administrator at creation or reset, expires after 24 hours, and
a session opened with it may only change it. A user without a row has no password and cannot
sign in with one. The hash is computed before the transaction that stores it, so no write lock is
held while Argon2id runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from orvex.access.storage.instants import instant
from orvex.access.storage.store import Transaction

TEMPORARY_LIFETIME = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Credential:
    hash: str
    temporary: bool
    expires_at: str | None

    def expired(self, now: str) -> bool:
        """Whether a temporary password is past its 24 hours (instants compare as text)."""
        return self.expires_at is not None and now >= self.expires_at


def of(transaction: Transaction, user_id: str) -> Credential | None:
    found = transaction.rows(
        "SELECT hash, temporary, expires_at FROM credentials WHERE user_id = :user",
        user=user_id,
    )
    if not found:
        return None
    hash, temporary, expires = found[0]
    return Credential(str(hash), bool(temporary), None if expires is None else str(expires))


def store(
    transaction: Transaction, user_id: str, hash: str, now: datetime, *, temporary: bool
) -> None:
    """Set the user's password: a temporary one expires `TEMPORARY_LIFETIME` from `now`."""
    expires = instant(now + TEMPORARY_LIFETIME) if temporary else None
    transaction.run(
        "DELETE FROM credentials WHERE user_id = :user",
        user=user_id,
    )
    transaction.run(
        "INSERT INTO credentials (user_id, hash, temporary, expires_at, changed_at) "
        "VALUES (:user, :hash, :temporary, :expires, :now)",
        user=user_id,
        hash=hash,
        temporary=temporary,
        expires=expires,
        now=instant(now),
    )


def replace_if_unchanged(
    transaction: Transaction,
    user_id: str,
    seen: str,
    hash: str,
    now: datetime,
    *,
    permanent: bool = False,
) -> bool:
    """Replace the hash that just verified, unless it changed meanwhile: with a stronger hash of
    the same password, or (`permanent`) with the user's own new password."""
    if permanent:
        changed = transaction.rows(
            "UPDATE credentials SET hash = :hash, changed_at = :now, temporary = :no, "
            "expires_at = NULL WHERE user_id = :user AND hash = :seen RETURNING user_id",
            user=user_id,
            seen=seen,
            hash=hash,
            now=instant(now),
            no=False,
        )
    else:
        changed = transaction.rows(
            "UPDATE credentials SET hash = :hash, changed_at = :now "
            "WHERE user_id = :user AND hash = :seen RETURNING user_id",
            user=user_id,
            seen=seen,
            hash=hash,
            now=instant(now),
        )
    return bool(changed)
