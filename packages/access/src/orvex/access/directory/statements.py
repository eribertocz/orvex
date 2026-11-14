"""The directory's SQL, one function per statement, shared by its operations and its bootstrap
(application).

Every statement is plain SQL with `:name` parameters and runs inside the caller's transaction.
The locking reads are in `locks.py`; the counts that a rule decides on are here, read after
those locks.
"""

from __future__ import annotations

from orvex.access.directory.names import checked_role
from orvex.access.directory.records import Role, User
from orvex.access.directory.rules import Caller
from orvex.access.storage.store import Row, Transaction

_USER = (
    "SELECT id, user_name, display_name, state, administrator, authz_version, created_at, "
    "changed_at FROM users"
)


def _user(row: Row) -> User:
    id, user_name, display_name, state, administrator, version, created, changed = row
    return User(
        id=str(id),
        user_name=str(user_name),
        display_name=str(display_name),
        state="active" if state == "active" else "deactivated",
        administrator=bool(administrator),
        authz_version=int(str(version)),
        created_at=str(created),
        changed_at=str(changed),
    )


def user(transaction: Transaction, user_id: str) -> User | None:
    found = transaction.rows(f"{_USER} WHERE id = :id", id=user_id)
    return _user(found[0]) if found else None


def user_by_name(transaction: Transaction, user_name: str) -> User | None:
    found = transaction.rows(f"{_USER} WHERE lower(user_name) = lower(:name)", name=user_name)
    return _user(found[0]) if found else None


def users_after(transaction: Transaction, after: str, limit: int) -> list[User]:
    """Up to `limit` users whose lower-case user name sorts after `after`, in that order."""
    rows = transaction.rows(
        f"{_USER} WHERE lower(user_name) > :after ORDER BY lower(user_name) LIMIT :limit",
        after=after,
        limit=limit,
    )
    return [_user(row) for row in rows]


def members(transaction: Transaction, project: str) -> list[tuple[User, Role]]:
    """The project's members and their roles, by user name."""
    rows = transaction.rows(
        "SELECT u.id, u.user_name, u.display_name, u.state, u.administrator, u.authz_version, "
        "u.created_at, u.changed_at, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.project = :project ORDER BY lower(u.user_name)",
        project=project,
    )
    return [(_user(row[:8]), checked_role(str(row[8]))) for row in rows]


def group_names(transaction: Transaction, project: str) -> list[str]:
    rows = transaction.rows(
        "SELECT name FROM project_groups WHERE project = :project ORDER BY name", project=project
    )
    return [str(row[0]) for row in rows]


def role(transaction: Transaction, project: str, user_id: str) -> Role | None:
    found = transaction.rows(
        "SELECT role FROM memberships WHERE project = :project AND user_id = :user",
        project=project,
        user=user_id,
    )
    return checked_role(str(found[0][0])) if found else None


def caller(transaction: Transaction, user_id: str, project: str | None = None) -> Caller:
    """The caller's facts; an unknown caller is an inactive one, who may do nothing."""
    found = user(transaction, user_id)
    if found is None:
        return Caller(user_id=user_id, active=False, administrator=False, role=None)
    held = role(transaction, project, user_id) if project is not None else None
    return Caller(found.id, found.active, found.administrator, held)


def insert_user(
    transaction: Transaction,
    id: str,
    user_name: str,
    display_name: str,
    administrator: bool,
    now: str,
) -> None:
    transaction.run(
        "INSERT INTO users (id, user_name, display_name, state, administrator, authz_version, "
        "created_at, changed_at) VALUES (:id, :name, :display, 'active', :admin, 1, :now, :now)",
        id=id,
        name=user_name,
        display=display_name,
        admin=administrator,
        now=now,
    )


def bump(transaction: Transaction, user_id: str, now: str) -> None:
    """What the user may do changed: their sessions end (#438)."""
    transaction.run(
        "UPDATE users SET authz_version = authz_version + 1, changed_at = :now WHERE id = :id",
        id=user_id,
        now=now,
    )


def set_state(transaction: Transaction, user_id: str, active: bool, now: str) -> None:
    transaction.run(
        "UPDATE users SET state = :state WHERE id = :id",
        id=user_id,
        state="active" if active else "deactivated",
    )
    bump(transaction, user_id, now)


def set_administrator(transaction: Transaction, user_id: str, value: bool, now: str) -> None:
    transaction.run(
        "UPDATE users SET administrator = :value WHERE id = :id", id=user_id, value=value
    )
    bump(transaction, user_id, now)


def other_active_builders(transaction: Transaction, project: str, excluding: str) -> int:
    ((count,),) = transaction.rows(
        "SELECT count(*) FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.project = :project AND m.role = 'builder' AND u.state = 'active' "
        "AND m.user_id <> :excluding",
        project=project,
        excluding=excluding,
    )
    return int(str(count))


def has_active_builder(transaction: Transaction, project: str) -> bool:
    return other_active_builders(transaction, project, excluding="") > 0


def builder_projects(transaction: Transaction, user_id: str) -> list[str]:
    """The projects where the user is a Builder, in a fixed order, so locks never cross."""
    found = transaction.rows(
        "SELECT project FROM memberships WHERE user_id = :user AND role = 'builder' "
        "ORDER BY project",
        user=user_id,
    )
    return [str(project) for (project,) in found]


def other_active_administrators(transaction: Transaction, excluding: str) -> int:
    ((count,),) = transaction.rows(
        "SELECT count(*) FROM users WHERE administrator = :yes AND state = 'active' "
        "AND id <> :excluding",
        yes=True,
        excluding=excluding,
    )
    return int(str(count))


def insert_membership(transaction: Transaction, project: str, user_id: str, role: Role) -> None:
    transaction.run(
        "INSERT INTO memberships (project, user_id, role) VALUES (:project, :user, :role)",
        project=project,
        user=user_id,
        role=role,
    )


def update_role(transaction: Transaction, project: str, user_id: str, role: Role) -> None:
    transaction.run(
        "UPDATE memberships SET role = :role WHERE project = :project AND user_id = :user",
        project=project,
        user=user_id,
        role=role,
    )


def delete_membership(transaction: Transaction, project: str, user_id: str) -> None:
    """Remove the membership; its group memberships go with it (`ON DELETE CASCADE`)."""
    transaction.run(
        "DELETE FROM memberships WHERE project = :project AND user_id = :user",
        project=project,
        user=user_id,
    )


def group_exists(transaction: Transaction, project: str, name: str) -> bool:
    return bool(
        transaction.rows(
            "SELECT 1 FROM project_groups WHERE project = :project AND name = :name",
            project=project,
            name=name,
        )
    )


def insert_group(transaction: Transaction, project: str, name: str) -> None:
    transaction.run(
        "INSERT INTO project_groups (project, name) VALUES (:project, :name)",
        project=project,
        name=name,
    )


def group_members(transaction: Transaction, project: str, name: str) -> list[str]:
    found = transaction.rows(
        "SELECT user_id FROM group_members WHERE project = :project AND group_name = :name "
        "ORDER BY user_id",
        project=project,
        name=name,
    )
    return [str(member) for (member,) in found]


def delete_group(transaction: Transaction, project: str, name: str) -> None:
    transaction.run(
        "DELETE FROM project_groups WHERE project = :project AND name = :name",
        project=project,
        name=name,
    )


def insert_group_member(transaction: Transaction, project: str, name: str, user_id: str) -> None:
    transaction.run(
        "INSERT INTO group_members (project, group_name, user_id) VALUES (:project, :name, :user)",
        project=project,
        name=name,
        user=user_id,
    )


def delete_group_member(transaction: Transaction, project: str, name: str, user_id: str) -> bool:
    present = transaction.rows(
        "SELECT 1 FROM group_members WHERE project = :project AND group_name = :name "
        "AND user_id = :user",
        project=project,
        name=name,
        user=user_id,
    )
    transaction.run(
        "DELETE FROM group_members WHERE project = :project AND group_name = :name "
        "AND user_id = :user",
        project=project,
        name=name,
        user=user_id,
    )
    return bool(present)


def forget_grant(transaction: Transaction, project: str, name: str, user_id: str) -> None:
    """A Builder's own change to a membership makes it theirs: an OIDC mapping (#439) never takes
    back what a Builder set by hand."""
    transaction.run(
        "DELETE FROM oidc_grants WHERE user_id = :user AND project = :project "
        "AND group_name = :name",
        project=project,
        name=name,
        user=user_id,
    )


def groups_of(transaction: Transaction, project: str, user_id: str) -> frozenset[str]:
    found = transaction.rows(
        "SELECT group_name FROM group_members WHERE project = :project AND user_id = :user",
        project=project,
        user=user_id,
    )
    return frozenset(str(name) for (name,) in found)


def recovery_digest(transaction: Transaction) -> str | None:
    ((digest,),) = transaction.rows("SELECT recovery_digest FROM store_meta")
    return None if digest is None else str(digest)


def set_recovery_digest(transaction: Transaction, digest: str) -> None:
    transaction.run("UPDATE store_meta SET recovery_digest = :digest", digest=digest)
