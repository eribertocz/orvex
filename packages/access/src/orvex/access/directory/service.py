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
  the rows it counts (`locks.py`), then counts again, so two concurrent changes never both pass.
  A deactivation that would leave a project without an active Builder gets `LAST_BUILDER`. The
  last active administrator or Builder lowering their own permissions gets `LAST_ADMINISTRATOR`
  or `LAST_BUILDER`; anyone else lowering their own gets `ACCESS_FORBIDDEN`.
- **Revocation.** Every change to what a user may do (a role, a group, the state, the flag)
  bumps that user's `authz_version` in the same transaction.
- **Audited** (#437). Every change records its event in its own transaction, so neither exists
  without the other. A refusal changes nothing and is recorded on its own (`access.refused`).
- A transaction the database gives up on (a deadlock, a busy database) is run again, a few
  times, from the start (`retry.py`).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from orvex.access.audit.log import AuditLog
from orvex.access.directory import locks, rules
from orvex.access.directory import statements as sql
from orvex.access.directory.names import (
    checked_display_name,
    checked_group,
    checked_id,
    checked_role,
    checked_user_name,
)
from orvex.access.directory.records import Role, User
from orvex.access.errors import USER_NAME_TAKEN, VALIDATION_FAILED, AccessError
from orvex.access.storage import retry
from orvex.access.storage.store import Store, Transaction, UniqueViolation


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
        audit: AuditLog,
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
            self._event(
                transaction, "user.created", actor, {"user": created, "via": "administrator"}
            )
            found = sql.user(transaction, created)
            if found is None:
                raise _invalid("user")
            return found

        return self._write("create_user", actor, None, None, work)

    def deactivate_user(self, actor: str, target: str) -> None:
        """Stop the user's access at once; their memberships stay, for the audit log."""

        def work(transaction: Transaction, now: str) -> None:
            self._lowering_oneself(transaction, actor, target, administrator=True, projects=None)
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
            self._event(transaction, "user.deactivated", actor, {"user": target})

        self._write("deactivate_user", actor, None, target, work)

    def reactivate_user(self, actor: str, target: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_users(sql.caller(transaction, actor), target))
            locks.lock_users(transaction, target)
            if not self._existing(transaction, target).active:
                sql.set_state(transaction, target, True, now)
                self._event(transaction, "user.reactivated", actor, {"user": target})

        self._write("reactivate_user", actor, None, target, work)

    def set_administrator(self, actor: str, target: str, value: bool) -> None:
        def work(transaction: Transaction, now: str) -> None:
            if not value:
                self._lowering_oneself(transaction, actor, target, administrator=True, projects=())
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
            kind = "user.administrator_granted" if value else "user.administrator_removed"
            self._event(transaction, kind, actor, {"user": target})

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
            details = {"user": target, "role": granted}
            self._event(transaction, "membership.added", actor, details, place)

        self._write("add_membership", actor, project, target, work)

    def change_role(self, actor: str, project: str, target: str, role: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            granted = checked_role(role)
            if granted != "builder":
                self._lowering_oneself(
                    transaction, actor, target, administrator=False, projects=(project,)
                )
            current = self._membership(transaction, actor, project, target, role=granted)
            if current == granted:
                return
            if current == "builder":
                self._keep_a_builder(transaction, project, target)
            sql.update_role(transaction, project, target, granted)
            sql.bump(transaction, target, now)
            details = {"user": target, "role": granted, "previous_role": current}
            self._event(transaction, "membership.changed", actor, details, project)

        self._write("change_role", actor, project, target, work)

    def remove_membership(self, actor: str, project: str, target: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            self._lowering_oneself(
                transaction, actor, target, administrator=False, projects=(project,)
            )
            current = self._membership(transaction, actor, project, target, role=None)
            if current == "builder":
                self._keep_a_builder(transaction, project, target)
            sql.delete_membership(transaction, project, target)
            sql.bump(transaction, target, now)
            details = {"user": target, "previous_role": current}
            self._event(transaction, "membership.removed", actor, details, project)

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
            self._event(transaction, "group.created", actor, {"group": group}, project)

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
            details = {"group": name, "members": len(members)}
            self._event(transaction, "group.deleted", actor, details, project)

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
            details = {"group": name, "user": member}
            self._event(transaction, "group.member_added", actor, details, project)

        self._write("add_group_member", actor, project, member, work)

    def remove_group_member(self, actor: str, project: str, name: str, member: str) -> None:
        def work(transaction: Transaction, now: str) -> None:
            _refuse(rules.manage_groups(sql.caller(transaction, actor, project), member))
            if sql.delete_group_member(transaction, project, name, member):
                sql.bump(transaction, member, now)
                details = {"group": name, "user": member}
                self._event(transaction, "group.member_removed", actor, details, project)

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

    def _lowering_oneself(
        self,
        transaction: Transaction,
        actor: str,
        target: str,
        *,
        administrator: bool,
        projects: tuple[str, ...] | None,
    ) -> None:
        """A caller lowering their own permissions is refused (`rules.py`). The last active
        administrator, or a project's last active Builder, is told so with `LAST_ADMINISTRATOR`
        or `LAST_BUILDER` rather than `ACCESS_FORBIDDEN` (#436 R2, R5). `projects` None means
        every project where the caller is a Builder (a deactivation)."""
        if target != actor:
            return
        caller = sql.caller(transaction, actor)
        if not caller.active:
            return  # the rule refuses a deactivated caller
        locks.lock_users(transaction, actor, administrators=administrator)
        if administrator and caller.administrator:
            _refuse(
                rules.keeps_an_administrator(sql.other_active_administrators(transaction, actor))
            )
        places = sql.builder_projects(transaction, actor) if projects is None else projects
        for project in places:
            if sql.role(transaction, project, actor) == "builder":
                locks.lock_builders(transaction, project)
                _refuse(
                    rules.keeps_a_builder(sql.other_active_builders(transaction, project, actor))
                )

    def _keep_a_builder(self, transaction: Transaction, project: str, target: str) -> None:
        """Lowering `target` from Builder must leave the project an active Builder, unless
        `target` was not counted (a deactivated Builder)."""
        if not self._existing(transaction, target).active:
            return
        locks.lock_builders(transaction, project)
        _refuse(rules.keeps_a_builder(sql.other_active_builders(transaction, project, target)))

    def _event(
        self,
        transaction: Transaction,
        kind: str,
        actor: str,
        details: Mapping[str, object],
        project: str | None = None,
    ) -> None:
        """The change's audit event, in the change's own transaction (#437)."""
        self._audit.record(transaction, kind, actor, details, project=project)

    def _write[T](
        self,
        operation: str,
        actor: str,
        project: str | None,
        target: str | None,
        work: Callable[[Transaction, str], T],
    ) -> T:
        try:
            done = retry.write(
                self._store, lambda transaction: work(transaction, instant(self._clock()))
            )
        except AccessError as error:
            if error.code.startswith("AUDIT_"):
                self._audit.unavailable()  # the log itself failed: no second attempt through it
            else:
                self._audit.refusal(operation, actor, error.code, project=project, target=target)
            raise
        self._audit.committed()
        return done
