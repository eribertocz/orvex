"""Who may change what in the directory (domain): the rules of #436 items 6 and 10.

Each rule takes facts read inside the operation's transaction and answers with the code that
refuses the change, or `None`. A rule never reads anything itself.

- Only an active administrator manages users and the administrator flag.
- A project's memberships are its active Builder's to manage. The one exception is the project's
  first Builder, whom an administrator assigns while the project has no active Builder.
- A project's groups are its active Builder's to manage.
- No caller changes their own role, group memberships, state or administrator flag: every
  change to what a user may do is someone else's decision. The service first tells the last
  active administrator or Builder that they are the last (`keeps_an_administrator`,
  `keeps_a_builder`), which is the more useful answer.
- A deactivated caller may do nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from orvex.access.directory.records import Role
from orvex.access.errors import ACCESS_FORBIDDEN, LAST_ADMINISTRATOR, LAST_BUILDER


@dataclass(frozen=True, slots=True)
class Caller:
    """The facts about the caller that the rules need, read in the operation's transaction."""

    user_id: str
    active: bool
    administrator: bool
    role: Role | None  # in the project the operation concerns, when it concerns one


def manage_users(caller: Caller, target: str | None = None) -> str | None:
    """Create, deactivate or reactivate a user, or change the administrator flag."""
    if not (caller.active and caller.administrator) or target == caller.user_id:
        return ACCESS_FORBIDDEN
    return None


def manage_membership(
    caller: Caller, target: str, *, adding: bool, role: Role | None, project_has_builder: bool
) -> str | None:
    """Add (`adding`), change or remove (`role` None) the membership of `target`."""
    if not caller.active or target == caller.user_id:
        return ACCESS_FORBIDDEN
    if caller.role == "builder":
        return None
    first_builder = adding and role == "builder" and not project_has_builder
    if caller.administrator and first_builder:
        return None
    return ACCESS_FORBIDDEN


def manage_groups(caller: Caller, member: str | None = None) -> str | None:
    """Create or delete a group, or add or remove `member`."""
    if not caller.active or caller.role != "builder" or member == caller.user_id:
        return ACCESS_FORBIDDEN
    return None


def keeps_a_builder(other_active_builders: int) -> str | None:
    """A project keeps at least one active Builder."""
    return LAST_BUILDER if other_active_builders < 1 else None


def keeps_an_administrator(other_active_administrators: int) -> str | None:
    """The instance keeps at least one active administrator."""
    return LAST_ADMINISTRATOR if other_active_administrators < 1 else None
