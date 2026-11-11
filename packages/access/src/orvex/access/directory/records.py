"""The records the directory returns, and the roles it knows (domain)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The project roles of `security.md` §3.1, as `security-context` v1 spells them.
type Role = Literal["builder", "author", "viewer"]
ROLES: tuple[Role, ...] = ("builder", "author", "viewer")

type UserState = Literal["active", "deactivated"]


@dataclass(frozen=True, slots=True)
class User:
    """A user of the instance. `authz_version` grows with every change to what the user may do
    (role, groups, state, administrator flag); a session (#438) ends when it changes."""

    id: str
    user_name: str
    display_name: str
    state: UserState
    administrator: bool
    authz_version: int
    created_at: str
    changed_at: str

    @property
    def active(self) -> bool:
        return self.state == "active"
