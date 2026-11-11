"""Links from an OIDC identity to a directory user (application, #439, #449 Q1 A and Q5 A).

An identity is the pair (issuer, subject), never an e-mail. It signs in only once an
administrator has linked it to an existing user; nothing links itself, and no user name or
e-mail ever matches one automatically. An administrator never links or unlinks their own
user. Each change ends the user's sessions (`authz_version`) and is audited
(`user.oidc_linked`, `user.oidc_unlinked`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import rules
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.rules import refused
from orvex.access.errors import VALIDATION_FAILED, AccessError
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

    def link(self, actor: str, user: str, provider: str, subject: str) -> None:
        issuer = self._configuration.provider(provider).issuer
        if not 1 <= len(subject) <= 255 or not subject.isprintable():
            raise _invalid("subject")

        def work(transaction: Transaction) -> None:
            refused(rules.manage_users(directory_sql.caller(transaction, actor), user))
            if directory_sql.user(transaction, user) is None:
                raise _invalid("user")
            now = instant(self._clock())
            try:
                transaction.run(
                    "INSERT INTO oidc_identities (issuer, subject, provider, user_id, linked_at) "
                    "VALUES (:issuer, :subject, :provider, :user, :now)",
                    issuer=issuer,
                    subject=subject,
                    provider=provider,
                    user=user,
                    now=now,
                )
            except UniqueViolation as error:
                raise _invalid("identity") from error
            directory_sql.bump(transaction, user, now)
            self._audit.record(
                transaction, "user.oidc_linked", actor, {"user": user, "provider": provider}
            )

        audited(self._store, self._audit, "oidc_link", actor, work, target=user)

    def unlink(self, actor: str, user: str, provider: str, subject: str) -> None:
        issuer = self._configuration.provider(provider).issuer

        def work(transaction: Transaction) -> None:
            refused(rules.manage_users(directory_sql.caller(transaction, actor), user))
            gone = transaction.rows(
                "DELETE FROM oidc_identities WHERE issuer = :issuer AND subject = :subject "
                "AND user_id = :user RETURNING user_id",
                issuer=issuer,
                subject=subject,
                user=user,
            )
            if not gone:
                raise _invalid("identity")
            directory_sql.bump(transaction, user, instant(self._clock()))
            self._audit.record(
                transaction, "user.oidc_unlinked", actor, {"user": user, "provider": provider}
            )

        audited(self._store, self._audit, "oidc_unlink", actor, work, target=user)
