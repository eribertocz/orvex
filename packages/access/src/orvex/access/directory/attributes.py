"""Setting and removing per-project user attributes (application, #440, option A): the
project's Builder's operations. Reading them is `attribute_sources`.

- **Who.** Only an active Builder of the project, for another active member of it
  (`rules.manage_attributes`). A Builder never changes their own attributes, so nobody widens
  their own access. The administrator flag grants no attribute rights. Every refusal, a
  non-member or deactivated target included, is `ACCESS_FORBIDDEN`.
- **Limits.** At most 64 attributes per user in a project. A membership's attributes go with it
  (`ON DELETE CASCADE`), so a re-admission starts with none.
- **Audit.** Every set and removal is recorded with its event in one transaction. Every
  refusal, an invalid name or value included, is checked inside the audited operation and
  audited apart. The details name the user, the attribute, its type and the SHA-256 of its
  canonical value, never the value in clear (Owner, #475): the log shows that a value was set
  or changed. A value from a small set (a boolean, a short code) can be guessed from its hash.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import attribute_values, rules
from orvex.access.directory import statements as sql
from orvex.access.directory.attribute_sources import stored_attributes
from orvex.access.directory.rules import refused
from orvex.access.storage.instants import instant
from orvex.access.storage.store import Store, Transaction


def _details(user: str, name: str, attribute: Mapping[str, Any] | None) -> dict[str, Any]:
    details: dict[str, Any] = {"user": user, "name": name}
    if attribute is not None:
        kind, text = attribute_values.stored(dict(attribute))
        details["type"] = kind
        details["value_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return details


class Attributes:
    """A Builder's operations on the attributes of the members of their project."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def _allowed(self, transaction: Transaction, actor: str, project: str, user: str) -> None:
        target = sql.user(transaction, user)
        member = target is not None and target.active and sql.role(transaction, project, user)
        refused(
            rules.manage_attributes(sql.caller(transaction, actor, project), user, bool(member))
        )

    def set(self, actor: str, project: str, user: str, name: str, kind: str, value: object) -> None:
        def work(transaction: Transaction) -> None:
            self._allowed(transaction, actor, project, user)
            checked_name = attribute_values.checked_name(name)
            attribute = attribute_values.checked(kind, value)
            stored_kind, text = attribute_values.stored(attribute)
            held = transaction.rows(
                "SELECT name FROM attributes WHERE project = :project AND user_id = :user",
                project=project,
                user=user,
            )
            names = {str(found) for (found,) in held}
            if checked_name not in names and len(names) >= attribute_values.MAXIMUM_PER_USER:
                raise attribute_values.invalid("name")  # the 65th
            transaction.run(
                "DELETE FROM attributes WHERE project = :project AND user_id = :user "
                "AND name = :name",
                project=project,
                user=user,
                name=checked_name,
            )
            transaction.run(
                "INSERT INTO attributes (project, user_id, name, type, value, set_at) "
                "VALUES (:project, :user, :name, :type, :value, :now)",
                project=project,
                user=user,
                name=checked_name,
                type=stored_kind,
                value=text,
                now=instant(self._clock()),
            )
            self._audit.record(
                transaction,
                "attribute.set",
                actor,
                _details(user, checked_name, attribute),
                project=project,
            )

        audited(
            self._store, self._audit, "set_attribute", actor, work, project=project, target=user
        )

    def remove(self, actor: str, project: str, user: str, name: str) -> None:
        def work(transaction: Transaction) -> None:
            self._allowed(transaction, actor, project, user)
            checked_name = attribute_values.checked_name(name)
            gone = transaction.rows(
                "DELETE FROM attributes WHERE project = :project AND user_id = :user "
                "AND name = :name RETURNING name",
                project=project,
                user=user,
                name=checked_name,
            )
            if not gone:
                raise attribute_values.invalid("name")
            self._audit.record(
                transaction,
                "attribute.removed",
                actor,
                _details(user, checked_name, None),
                project=project,
            )

        audited(
            self._store, self._audit, "remove_attribute", actor, work, project=project, target=user
        )

    def of(self, user: str, project: str) -> dict[str, dict[str, Any]]:
        with self._store.transaction(read_only=True) as transaction:
            return stored_attributes(transaction, user, project)
