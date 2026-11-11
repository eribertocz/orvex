"""Which sessions came from an OIDC provider, and when their groups were last read (application,
#439). A row lives exactly as long as its session (`ON DELETE CASCADE`); a session that gets a new
id (a password change, #438) is given its old row again."""

from __future__ import annotations

from datetime import datetime

from orvex.access.authentication.sessions import session_hash
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Transaction


def record(
    transaction: Transaction,
    session_id: str,
    provider: str,
    issuer: str,
    subject: str,
    now: datetime,
) -> None:
    transaction.run(
        "INSERT INTO oidc_sessions (session, provider, issuer, subject, refreshed_at) "
        "VALUES (:session, :provider, :issuer, :subject, :now)",
        session=session_hash(session_id),
        provider=provider,
        issuer=issuer,
        subject=subject,
        now=instant(now),
    )


def of(transaction: Transaction, held: str) -> tuple[str, str, str, datetime] | None:
    """(provider, issuer, subject, refreshed_at) of the session whose hash is `held`."""
    found = transaction.rows(
        "SELECT provider, issuer, subject, refreshed_at FROM oidc_sessions "
        "WHERE session = :session",
        session=held,
    )
    if not found:
        return None
    provider, issuer, subject, refreshed = found[0]
    return str(provider), str(issuer), str(subject), parsed(str(refreshed))
