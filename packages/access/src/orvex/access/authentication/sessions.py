"""Server-side sessions (application, #438): a random 256-bit id for the browser, its SHA-256
alone in the store, and every request checked against the user as they are now.

A session ends, and its row is removed, when it is past its absolute lifetime, idle too long,
logged out, its user deactivated, or what its user may do changed (`authz_version`, #436). An id
that is unknown, ended or expired gets the same answer, which never says why; the audit log
does (`auth.session_expired`). The idle clock is written at most once a minute, so a session may
end up to a minute early, never late.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from orvex.access.authentication.settings import SignInSettings
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.records import User
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Transaction

ID_BYTES = 32
TOUCH_EVERY = timedelta(minutes=1)

type Ending = Literal["idle", "absolute", "revoked", "permissions_changed"]


@dataclass(frozen=True, slots=True)
class Session:
    """A live session: its user as they are now, and whether it may only change the password."""

    user: User
    password_change_only: bool


@dataclass(frozen=True, slots=True)
class Ended:
    """A session that just ended; `user` is who held it, for the audit log."""

    user: str
    reason: Ending


def session_hash(session_id: str) -> str:
    """The only form of a session id the store holds."""
    return hashlib.sha256(session_id.encode()).hexdigest()


def issue(
    transaction: Transaction, user: User, now: datetime, *, password_change_only: bool
) -> str:
    """A new session for `user`; the id is returned once and never stored."""
    session_id = secrets.token_urlsafe(ID_BYTES)
    transaction.run(
        "INSERT INTO sessions (id_hash, user_id, authz_version, created_at, seen_at, "
        "password_change_only) VALUES (:id, :user, :version, :now, :now, :only)",
        id=session_hash(session_id),
        user=user.id,
        version=user.authz_version,
        now=instant(now),
        only=password_change_only,
    )
    return session_id


class NeedsWrite:
    """A read-only check found a session to end or to touch: check again in a write
    transaction. Most requests are answered without taking the write lock."""


NEEDS_WRITE = NeedsWrite()


def check(
    transaction: Transaction,
    session_id: str,
    now: datetime,
    settings: SignInSettings,
    *,
    writable: bool,
) -> Session | Ended | NeedsWrite | None:
    """The live session `session_id` names, the reason it just ended, or None (unknown)."""
    if len(session_id) > 2 * ID_BYTES:
        return None
    found = transaction.rows(
        "SELECT user_id, authz_version, created_at, seen_at, password_change_only "
        "FROM sessions WHERE id_hash = :id",
        id=session_hash(session_id),
    )
    if not found:
        return None
    user_id, version, created, seen, only = found[0]
    holder = str(user_id)
    user = directory_sql.user(transaction, holder)
    reason: Ending | None = None
    if user is None or not user.active:
        reason = "revoked"
    elif int(str(version)) != user.authz_version:
        reason = "permissions_changed"
    elif now >= parsed(str(created)) + settings.absolute:
        reason = "absolute"
    elif now >= parsed(str(seen)) + settings.idle:
        reason = "idle"
    touch = now >= parsed(str(seen)) + TOUCH_EVERY
    if not writable and (reason is not None or user is None or touch):
        return NEEDS_WRITE
    if reason is not None or user is None:
        end(transaction, session_id)
        return Ended(holder, reason or "revoked")
    if touch:
        transaction.run(
            "UPDATE sessions SET seen_at = :now WHERE id_hash = :id",
            id=session_hash(session_id),
            now=instant(now),
        )
    return Session(user, bool(only))


def end(transaction: Transaction, session_id: str) -> str | None:
    """End one session; return its user, or None when there was no such session."""
    gone = transaction.rows(
        "DELETE FROM sessions WHERE id_hash = :id RETURNING user_id", id=session_hash(session_id)
    )
    return str(gone[0][0]) if gone else None


def end_all(transaction: Transaction, user_id: str) -> None:
    """End every session of the user (a password reset, a password change before re-issuing)."""
    transaction.run("DELETE FROM sessions WHERE user_id = :user", user=user_id)
