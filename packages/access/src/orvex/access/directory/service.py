"""The directory's operations: each checked against its caller and done in one transaction
(application, #436).

- **Current state, every time.** Every decision reads the caller, the target and the counts in
  the operation's own transaction. `role_of` and `groups_of` read the store at the moment of the
  request, and nothing about roles is cached here.
- **Fail closed.** An unknown or deactivated caller may do nothing. A refusal changes nothing,
  is reported to the audit hook, and raises `AccessError` with its code.
- **No escalation** (`rules.py`). The administrator flag is an administrator's to grant. A
  project's memberships and groups are its Builder's. No caller changes what they themselves may
  do.
- **The last Builder and the last administrator.** A change that could lower a count first locks
  the rows it counts (`locks.py`), then counts again. Two concurrent changes therefore never both
  pass, and one of them gets `LAST_BUILDER` or `LAST_ADMINISTRATOR`.
- **Revocation.** Every change to what a user may do (a role, a group, the state, the flag)
  bumps that user's `authz_version` in the same transaction.
- A transaction the database gives up on (a deadlock, a busy database) is run again, a few
  times, from the start.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime

from orvex.access.directory import locks, rules
from orvex.access.directory import statements as sql
from orvex.access.directory.events import AuditHook, DirectoryEvent
from orvex.access.directory.names import (
    checked_display_name,
    checked_group,
    checked_id,
    checked_role,
    checked_user_name,
)
from orvex.access.directory.records import Role, User
from orvex.access.directory.store import Store, Transaction, TransientConflict, UniqueViolation
from orvex.access.errors import USER_NAME_TAKEN, VALIDATION_FAILED, AccessError

ATTEMPTS = 5


def instant(moment: datetime) -> str:
    """The stored form of an instant: UTC, microseconds, `Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_user_id() -> str:
    """128 random bits in the alphabet of `security-context` v1 ids; never reused."""
    return secrets.token_urlsafe(16)


def _refuse(code: str | None) -> None:
    if code is not None:
        raise AccessError(code, "The operation is not allowed.")


def _invalid(field: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The {field} is not valid.", {"field": field})


class Directory:
    """Users, project memberships, groups and administrators of one instance."""

    def __init__(
        self,
        store: Store,
        audit: AuditHook,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = new_user_id,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock
        self._new_id = new_id

    # --- reading -------------------------------------------------------------------------------

    def user(self, user_id: str) -> User | None:
        with self._store.transaction(read_only=True) as transaction:
            return sql.user(transaction, user_id)

    def role_of(self, user_id: str, project: str) -> Role | None:
        """The user's role in the project now; none for a deactivated or unknown user, and none
        without a membership: a missing row never means a role."""
        with self._store.transaction(read_only=True) as transaction:
            found = sql.user(transaction, user_id)
            if found is None or not found.active:
                return None
            return sql.role(transaction, project, user_id)

    def groups_of(self, user_id: str, project: str) -> frozenset[str]:
        """The user's groups in the project now; none for a deactivated or unknown user."""
        with self._store.transaction(read_only=True) as transaction:
            found = sql.user(transaction, user_id)
            if found is None or not found.active:
                return frozenset()
            return sql.groups_of(transaction, project, user_id)

    # --- users and the administrator flag (administrators only) --------------------------------

    def create_user(self, actor: str, user_name: str, display_name: str) -> User:
        def work(transaction: Transaction, now: str) -> User:
            _refuse(rules.manage_users(sql.caller(transaction, actor)))
            name = checked_user_name(user_name)
            display = checked_display_name(display_name)
            if sql.user_by_name(transaction, name) is not None:
                raise AccessError(USER_NAME_TAKEN, "That user name is taken.")
            created = self._new_id()
            try:
                sql.insert_user(transaction, created, name, display, False, now)
            except UniqueViolation as error:
                raise AccessError(USER_NAME_TAKEN, "That user name is taken.") from error
            found = sql.user(transaction, created)
            if found is None:
                raise _invalid("user")
            return found

        return self._write("create_user", actor, None, None, work)

    def deactivate_user(self, actor: str, target: str) -> None:
        """Stop the user's access at once; their memberships stay, for the audit log."""

        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_users(sql.caller(transaction, actor), target))
            locks.lock_users(transaction, target, administrators=True)
            found = self._existing(transaction, target)
            if not found.active:
                return
            for project in sql.builder_projects(transaction, target):
                locks.lock_builders(transaction, project)
                _refuse(
                    rules.keeps_a_builder(sql.other_active_builders(transaction, project, target))
                )
            if found.administrator:
                _refuse(
                    rules.keeps_an_administrator(
                        sql.other_active_administrators(transaction, target)
                    )
                )
            sql.set_state(transaction, target, False, now)

        self._write("deactivate_user", actor, None, target, work)

    def reactivate_user(self, actor: str, target: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_users(sql.caller(transaction, actor), target))
            locks.lock_users(transaction, target)
            if not self._existing(transaction, target).active:
                sql.set_state(transaction, target, True, now)

        self._write("reactivate_user", actor, None, target, work)

    def set_administrator(self, actor: str, target: str, value: bool) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_users(sql.caller(transaction, actor), target))
            locks.lock_users(transaction, target, administrators=True)
            found = self._existing(transaction, target)
            if found.administrator == value:
                return
            if not value and found.active:
                _refuse(
                    rules.keeps_an_administrator(
                        sql.other_active_administrators(transaction, target)
                    )
                )
            sql.set_administrator(transaction, target, value, now)

        self._write("set_administrator", actor, None, target, work)

    # --- memberships (the project's Builder; an administrator for the first Builder) ----------

    def add_membership(self, actor: str, project: str, target: str, role: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            granted = checked_role(role)
            place = checked_id(project, "project")
            caller = sql.caller(transaction, actor, place)
            has_builder = sql.has_active_builder(transaction, place)
            _refuse(
                rules.manage_membership(
                    caller, target, adding=True, role=granted, project_has_builder=has_builder
                )
            )
            locks.lock_users(transaction, target)
            if not self._existing(transaction, target).active:
                raise _invalid("user")
            if sql.role(transaction, place, target) is not None:
                raise _invalid("membership")
            try:
                sql.insert_membership(transaction, place, target, granted)
            except UniqueViolation as error:
                raise _invalid("membership") from error
            sql.bump(transaction, target, now)

        self._write("add_membership", actor, project, target, work)

    def change_role(self, actor: str, project: str, target: str, role: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            granted = checked_role(role)
            current = self._membership(transaction, actor, project, target, role=granted)
            if current == granted:
                return
            if current == "builder":
                self._keep_a_builder(transaction, project, target)
            sql.update_role(transaction, project, target, granted)
            sql.bump(transaction, target, now)

        self._write("change_role", actor, project, target, work)

    def remove_membership(self, actor: str, project: str, target: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            current = self._membership(transaction, actor, project, target, role=None)
            if current == "builder":
                self._keep_a_builder(transaction, project, target)
            sql.delete_membership(transaction, project, target)
            sql.bump(transaction, target, now)

        self._write("remove_membership", actor, project, target, work)

    # --- groups (the project's Builder) --------------------------------------------------------

    def create_group(self, actor: str, project: str, name: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            group = checked_group(name)
            _refuse(rules.manage_groups(sql.caller(transaction, actor, project)))
            if sql.group_exists(transaction, project, group):
                raise _invalid("group")
            try:
                sql.insert_group(transaction, project, group)
            except UniqueViolation as error:
                raise _invalid("group") from error

        self._write("create_group", actor, project, None, work)

    def delete_group(self, actor: str, project: str, name: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            caller = sql.caller(transaction, actor, project)
            _refuse(rules.manage_groups(caller))
            if not sql.group_exists(transaction, project, name):
                raise _invalid("group")
            members = sql.group_members(transaction, project, name)
            _refuse(rules.manage_groups(caller, actor if actor in members else None))
            sql.delete_group(transaction, project, name)
            for member in members:
                sql.bump(transaction, member, now)

        self._write("delete_group", actor, project, None, work)

    def add_group_member(self, actor: str, project: str, name: str, member: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_groups(sql.caller(transaction, actor, project), member))
            if not sql.group_exists(transaction, project, name):
                raise _invalid("group")
            if sql.role(transaction, project, member) is None:
                raise _invalid("membership")
            if name in sql.groups_of(transaction, project, member):
                return
            try:
                sql.insert_group_member(transaction, project, name, member)
            except UniqueViolation:
                return  # a concurrent call added the same member
            sql.bump(transaction, member, now)

        self._write("add_group_member", actor, project, member, work)

    def remove_group_member(self, actor: str, project: str, name: str, member: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_groups(sql.caller(transaction, actor, project), member))
            if sql.delete_group_member(transaction, project, name, member):
                sql.bump(transaction, member, now)

        self._write("remove_group_member", actor, project, member, work)

    # --- shared steps --------------------------------------------------------------------------

    def _existing(self, transaction: Transaction, user_id: str) -> User:
        found = sql.user(transaction, user_id)
        if found is None:
            raise _invalid("user")
        return found

    def _membership(
        self, transaction: Transaction, actor: str, project: str, target: str, role: Role | None
    ) -> Role:
        """Check a change or removal of an existing membership; return the role it has now."""
        caller = sql.caller(transaction, actor, project)
        has_builder = sql.has_active_builder(transaction, project)
        _refuse(
            rules.manage_membership(
                caller, target, adding=False, role=role, project_has_builder=has_builder
            )
        )
        locks.lock_users(transaction, target)
        current = sql.role(transaction, project, target)
        if current is None:
            raise _invalid("membership")
        return current

    def _keep_a_builder(self, transaction: Transaction, project: str, target: str) -> None:
        """Lowering `target` from Builder must leave the project an active Builder, unless
        `target` was not counted (a deactivated Builder)."""
        if not self._existing(transaction, target).active:
            return
        locks.lock_builders(transaction, project)
        _refuse(rules.keeps_a_builder(sql.other_active_builders(transaction, project, target)))

    def _write[T](
        self,
        operation: str,
        actor: str,
        project: str | None,
        target: str | None,
        work: Callable[[Transaction, str], T],
    ) -> T:
        attempt = 1
        try:
            while True:
                try:
                    with self._store.transaction() as transaction:
                        return work(transaction, instant(self._clock()))
                except TransientConflict:
                    if attempt == ATTEMPTS:
                        raise
                    time.sleep(0.01 * attempt + secrets.randbelow(10) / 1000)
                    attempt += 1
        except AccessError as error:
            self._audit(DirectoryEvent(operation, "refused", actor, project, target, error.code))
            raise
