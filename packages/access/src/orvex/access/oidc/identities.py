"""Links from an OIDC identity to a directory user (application, #439, #449 Q1 A and Q5 A).

An identity is the pair (issuer, subject), never an e-mail. It signs in only once an
administrator has linked it to an existing user; nothing links itself, and no user name or
e-mail ever matches one automatically. An administrator never links or unlinks their own
user. Each change ends the user's sessions (`authz_version`) and is audited
(`user.oidc_linked`, `user.oidc_unlinked`).

A link is named by an opaque id (#446 precision 4), 128 random bits, so no subject is ever in a
route, a log line or an event. Only the administrator's list of a user's links shows the
subject. An id that is not that user's link answers as an unknown one (`IDENTITY_NOT_FOUND`).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import rules
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.rules import refused
from orvex.access.errors import (
    IDENTITY_NOT_FOUND,
    IDENTITY_TAKEN,
    USER_NOT_FOUND,
    VALIDATION_FAILED,
    AccessError,
    not_found,
)
from orvex.access.oidc.configuration import Configuration
from orvex.access.storage.instants import instant
from orvex.access.storage.store import Store, Transaction, UniqueViolation


def linked_user(transaction: Transaction, issuer: str, subject: str) -> str | None:
    found = transaction.rows(
        "SELECT user_id FROM oidc_identities WHERE issuer = :issuer AND subject = :subject",
        issuer=issuer,
        subject=subject,
    )
    return str(found[0][0]) if found else None


def _invalid(field: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The {field} is not valid.", {"field": field})


@dataclass(frozen=True, slots=True)
class Link:
    """One of a user's links, as the administrator lists them."""

    id: str
    provider: str
    subject: str
    linked_at: str


class Identities:
    """An administrator's links between provider identities and users."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        configuration: Configuration,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._configuration = configuration
        self._clock = clock

    def link(self, actor: str, user: str, provider: str, subject: str) -> str:
        """Link (provider, subject) to `user`; return the link's opaque id."""
        issuer = self._configuration.provider(provider).issuer
        if not 1 <= len(subject) <= 255 or not subject.isprintable():
            raise _invalid("subject")
        link_id = secrets.token_hex(16)

        def work(transaction: Transaction) -> None:
            refused(rules.manage_users(directory_sql.caller(transaction, actor), user))
            if directory_sql.user(transaction, user) is None:
                raise not_found(USER_NOT_FOUND)
            now = instant(self._clock())
            try:
                transaction.run(
                    "INSERT INTO oidc_identities "
                    "(issuer, subject, provider, user_id, linked_at, link_id) "
                    "VALUES (:issuer, :subject, :provider, :user, :now, :link)",
                    issuer=issuer,
                    subject=subject,
                    provider=provider,
                    user=user,
                    now=now,
                    link=link_id,
                )
            except UniqueViolation as error:
                raise AccessError(IDENTITY_TAKEN, "That identity is already linked.") from error
            directory_sql.bump(transaction, user, now)
            self._audit.record(
                transaction, "user.oidc_linked", actor, {"user": user, "provider": provider}
            )

        audited(self._store, self._audit, "oidc_link", actor, work, target=user)
        return link_id

    def unlink(self, actor: str, user: str, link_id: str) -> None:
        """Remove `user`'s link `link_id`; another user's id is as unknown as a missing one."""

        def work(transaction: Transaction) -> None:
            refused(rules.manage_users(directory_sql.caller(transaction, actor), user))
            gone = transaction.rows(
                "DELETE FROM oidc_identities WHERE link_id = :link AND user_id = :user "
                "RETURNING provider",
                link=link_id,
                user=user,
            )
            if not gone:
                raise not_found(IDENTITY_NOT_FOUND)
            directory_sql.bump(transaction, user, instant(self._clock()))
            self._audit.record(
                transaction,
                "user.oidc_unlinked",
                actor,
                {"user": user, "provider": str(gone[0][0])},
            )

        audited(self._store, self._audit, "oidc_unlink", actor, work, target=user)

    def links(self, actor: str, user: str) -> list[Link]:
        """`user`'s links, for an active administrator (their own included: a read)."""
        with self._store.transaction(read_only=True) as transaction:
            refused(rules.read_users(directory_sql.caller(transaction, actor)))
            if directory_sql.user(transaction, user) is None:
                raise not_found(USER_NOT_FOUND)
            rows = transaction.rows(
                "SELECT link_id, provider, subject, linked_at FROM oidc_identities "
                "WHERE user_id = :user ORDER BY linked_at, link_id",
                user=user,
            )
        return [Link(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]
