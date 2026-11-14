"""The directory's reads for the administration surfaces (application, #446): who may read what
is `rules.py`'s, checked inside the read's own transaction; nothing here changes the directory.

- **Users** are an administrator's to list (by user name, a page at a time) and to read.
- **Finding a user** (Q4 A, precision 2) is by exact user name only, compared without regard to
  case, for an administrator or any project's Builder. It answers the id and display name, or
  one `USER_NOT_FOUND` for an unknown, invalid or deactivated name alike. Each caller has
  `LOOKUP_BUDGET` lookups per `LOOKUP_WINDOW`; past it, `LOOKUP_LIMITED` for any name, so the
  budget tells nothing either. A found lookup records nothing; no name is written anywhere.
- **A project's members** are read by its Builder and an administrator; its groups and its
  members' attributes by its Builder. A user who is not a member is not found, as one who does
  not exist.
- A refusal by a rule or by the budget is recorded (`access.refused`); a user who is not found
  is not a refusal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from orvex.access.audit.log import AuditLog
from orvex.access.directory import rules
from orvex.access.directory import statements as sql
from orvex.access.directory.attribute_sources import stored_attributes
from orvex.access.directory.names import canonical_user_name
from orvex.access.directory.records import Role, User
from orvex.access.directory.rules import refused
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    LOOKUP_LIMITED,
    USER_NOT_FOUND,
    AccessError,
    not_found,
)
from orvex.access.storage import retry
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Store, Transaction

LOOKUP_BUDGET = 30
LOOKUP_WINDOW = timedelta(minutes=1)
PAGE = 100  # users per page


@dataclass(frozen=True, slots=True)
class Page:
    """One page of the instance's users; `after` names the next one, or None at the end."""

    users: list[User]
    after: str | None


@dataclass(frozen=True, slots=True)
class Found:
    """A user found by name: their id and display name, nothing else."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Member:
    user: User
    role: Role


class LookupLimited(AccessError):
    """Past the caller's lookup budget; `retry_after` in whole seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(LOOKUP_LIMITED, "Too many user lookups; try again later.")
        self.retry_after = retry_after


class DirectoryReads:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def allowed(self, actor: str, permission: str, project: str | None) -> bool:
        """Whether `actor` may reach an administration route of `permission` (#446): the read
        rule of its resource, checked again by the operation itself."""
        with self._store.transaction(read_only=True) as transaction:
            caller = sql.caller(transaction, actor, project)
            if permission == "administrator":
                code = rules.read_users(caller)
            elif permission == "finder":
                code = rules.find_user(caller, bool(sql.builder_projects(transaction, actor)))
            elif permission == "membership":
                code = rules.read_project(caller, True)
            elif permission == "builder":
                code = rules.read_project(caller, False)
            else:
                code = ACCESS_FORBIDDEN
        return code is None

    def users(self, actor: str, after: str | None = None) -> Page:
        def read(transaction: Transaction) -> Page:
            refused(rules.read_users(sql.caller(transaction, actor)))
            rows = sql.users_after(transaction, (after or "").lower(), PAGE + 1)
            more = len(rows) > PAGE
            shown = rows[:PAGE]
            return Page(shown, shown[-1].user_name.lower() if more else None)

        return self._read("list_users", actor, None, read)

    def user(self, actor: str, user_id: str) -> User:
        def read(transaction: Transaction) -> User:
            refused(rules.read_users(sql.caller(transaction, actor)))
            found = sql.user(transaction, user_id)
            if found is None:
                raise not_found(USER_NOT_FOUND)
            return found

        return self._read("read_user", actor, None, read)

    def find(self, actor: str, user_name: str) -> Found:
        now = self._clock()

        def spend(transaction: Transaction) -> None:
            caller = sql.caller(transaction, actor)
            builder = bool(sql.builder_projects(transaction, actor))
            refused(rules.find_user(caller, builder))
            _spend(transaction, actor, now)

        def read(transaction: Transaction) -> Found:
            name = canonical_user_name(user_name)
            found = sql.user_by_name(transaction, name) if name is not None else None
            if found is None or not found.active:
                raise not_found(USER_NOT_FOUND)
            return Found(found.id, found.display_name)

        try:  # the lookup is counted, committed, before the name is read: found or not
            retry.write(self._store, spend)
        except AccessError as error:
            self._refused("find_user", actor, error, None)
            raise
        return self._read("find_user", actor, None, read)

    def members(self, actor: str, project: str) -> list[Member]:
        def read(transaction: Transaction) -> list[Member]:
            refused(rules.read_project(sql.caller(transaction, actor, project), True))
            return [Member(user, role) for user, role in sql.members(transaction, project)]

        return self._read("list_members", actor, project, read)

    def groups(self, actor: str, project: str) -> dict[str, list[str]]:
        def read(transaction: Transaction) -> dict[str, list[str]]:
            refused(rules.read_project(sql.caller(transaction, actor, project), False))
            return {
                name: sql.group_members(transaction, project, name)
                for name in sql.group_names(transaction, project)
            }

        return self._read("list_groups", actor, project, read)

    def attributes(self, actor: str, project: str, member: str) -> dict[str, dict[str, Any]]:
        def read(transaction: Transaction) -> dict[str, dict[str, Any]]:
            refused(rules.read_project(sql.caller(transaction, actor, project), False))
            if sql.role(transaction, project, member) is None:
                raise not_found(USER_NOT_FOUND)  # not a member: as if no such user
            return stored_attributes(transaction, member, project)

        return self._read("list_attributes", actor, project, read)

    def _read[T](
        self, operation: str, actor: str, project: str | None, read: Callable[[Transaction], T]
    ) -> T:
        try:
            with self._store.transaction(read_only=True) as transaction:
                return read(transaction)
        except AccessError as error:
            self._refused(operation, actor, error, project)
            raise

    def _refused(self, operation: str, actor: str, error: AccessError, project: str | None) -> None:
        if error.code in {ACCESS_FORBIDDEN, LOOKUP_LIMITED}:
            self._audit.refusal(operation, actor, error.code, project=project)


def _spend(transaction: Transaction, actor: str, now: datetime) -> None:
    """Count one lookup in the caller's window, or refuse past the budget."""
    found = transaction.rows(
        "SELECT window_start, used FROM lookup_budgets WHERE user_id = :user", user=actor
    )
    if found and now - parsed(str(found[0][0])) < LOOKUP_WINDOW:
        used = int(str(found[0][1]))
        if used >= LOOKUP_BUDGET:
            left = LOOKUP_WINDOW - (now - parsed(str(found[0][0])))
            raise LookupLimited(max(1, int(left.total_seconds() + 0.999)))
        transaction.run(
            "UPDATE lookup_budgets SET used = :used WHERE user_id = :user",
            used=used + 1,
            user=actor,
        )
        return
    transaction.run("DELETE FROM lookup_budgets WHERE user_id = :user", user=actor)
    transaction.run(
        "INSERT INTO lookup_budgets (user_id, window_start, used) VALUES (:user, :now, 1)",
        user=actor,
        now=instant(now),
    )
