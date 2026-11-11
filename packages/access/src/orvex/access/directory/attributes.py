"""Per-project user attributes (application, #440, option A): set and removed by the project's
Builder, read by the context provider through the `AttributeSource` interface.

- **Who.** Only an active Builder of the project, for another active member of it
  (`rules.manage_attributes`). A Builder never changes their own attributes, so nobody widens
  their own access. The administrator flag grants no attribute rights.
- **Limits.** At most 64 attributes per user in a project. A membership's attributes go with it
  (`ON DELETE CASCADE`), so a re-admission starts with none.
- **Audit.** Every set and removal is recorded with its event in one transaction, and a refusal
  is audited apart. The details name the user, the attribute and its type, the SHA-256 of the
  value, and the value itself when it is at most 1,024 characters long. The value is the grant
  itself, so the log keeps it whenever it fits.
- **Reading back.** `stored_attributes` returns each attribute through `attribute_values.typed`.
  A stored value that fails the checks raises `UnreadableAttribute`: it is never dropped or
  repaired, and the caller builds no context (fail closed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from orvex.access.audit.log import AuditLog
from orvex.access.audit.operation import audited
from orvex.access.directory import attribute_values, rules
from orvex.access.directory import statements as sql
from orvex.access.directory.rules import refused
from orvex.access.errors import AccessError
from orvex.access.storage.instants import instant
from orvex.access.storage.store import Store, Transaction

_SHOWN = 1024


class UnreadableAttribute(Exception):
    """A stored attribute that fails the contract's checks: no context is built from it."""


class AttributeSource(Protocol):
    """Where a user's typed attributes in a project come from, read inside the caller's
    transaction. A later source (an identity provider's claims, say) is another implementation
    behind a fixed precedence: a value set by hand is never overwritten."""

    def attributes_for(
        self, transaction: Transaction, user: str, project: str
    ) -> dict[str, dict[str, Any]]: ...


def stored_attributes(
    transaction: Transaction, user: str, project: str
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name, kind, value in transaction.rows(
        "SELECT name, type, value FROM attributes WHERE project = :project AND user_id = :user",
        project=project,
        user=user,
    ):
        try:
            found[attribute_values.checked_name(str(name))] = attribute_values.typed(
                str(kind), str(value)
            )
        except AccessError as error:
            raise UnreadableAttribute(str(name)) from error
    return found


class ProjectAttributes:
    """The attributes a Builder set in the project: the one source in v1."""

    def attributes_for(
        self, transaction: Transaction, user: str, project: str
    ) -> dict[str, dict[str, Any]]:
        return stored_attributes(transaction, user, project)


def merged(
    sources: tuple[AttributeSource, ...], transaction: Transaction, user: str, project: str
) -> dict[str, dict[str, Any]]:
    """The first source's value of each name wins: the project's own attributes come first."""
    found: dict[str, dict[str, Any]] = {}
    for source in sources:
        for name, value in source.attributes_for(transaction, user, project).items():
            found.setdefault(name, value)
    if len(found) > attribute_values.MAXIMUM_PER_USER:
        raise UnreadableAttribute("too many attributes")
    return found


def _details(user: str, name: str, attribute: Mapping[str, Any] | None) -> dict[str, Any]:
    details: dict[str, Any] = {"user": user, "name": name}
    if attribute is not None:
        kind, text = attribute_values.stored(dict(attribute))
        details["type"] = kind
        details["value_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        if len(text) <= _SHOWN:
            details["value"] = text
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
        refused(rules.manage_attributes(sql.caller(transaction, actor, project), user))
        target = sql.user(transaction, user)
        if target is None or not target.active or sql.role(transaction, project, user) is None:
            raise attribute_values.invalid("user")

    def set(self, actor: str, project: str, user: str, name: str, kind: str, value: object) -> None:
        checked_name = attribute_values.checked_name(name)
        attribute = attribute_values.checked(kind, value)
        stored_kind, text = attribute_values.stored(attribute)

        def work(transaction: Transaction) -> None:
            self._allowed(transaction, actor, project, user)
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
        checked_name = attribute_values.checked_name(name)

        def work(transaction: Transaction) -> None:
            self._allowed(transaction, actor, project, user)
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
