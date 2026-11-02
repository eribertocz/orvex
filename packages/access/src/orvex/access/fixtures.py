"""A context provider over fixtures, for tests and the corpus runner (P2-09, #306).

The real provider (sessions, OIDC, tokens) is P3. This one builds each request's context anew
from its current fixtures, exactly as the real one will from current state, so the reuse rule
(`may_reuse`) is exercised the same way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orvex.access.context import (
    InvalidContext,
    SecurityContext,
    normalize_list,
    permission_fingerprint,
    read_context,
)


@dataclass(frozen=True, slots=True)
class Request:
    user: str
    project: str


@dataclass(slots=True)
class Member:
    """One user's standing in one project. `instance_admin` is the instance flag: it grants no
    data access and never reaches a context."""

    role: str
    groups: Iterable[str] = ()
    attributes: Mapping[str, object] = field(default_factory=dict)
    instance_admin: bool = False


def _attribute(value: object) -> dict[str, Any]:
    """A typed attribute from a fixture value: lists normalized here, exactly once."""
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, list | tuple | set | frozenset):
        return {"type": "list", "values": normalize_list(str(item) for item in value)}
    raise InvalidContext(f"a {type(value).__name__} is not an attribute value")


class FixtureContextProvider:
    """Contexts from in-memory fixtures; tests change them between requests."""

    def __init__(self, policy_fingerprint: str, mapping_snapshots: Mapping[str, str]) -> None:
        self.members: dict[tuple[str, str], Member] = {}
        self.policy_fingerprint = policy_fingerprint
        self.mapping_snapshots = dict(mapping_snapshots)

    def add(self, user: str, project: str, member: Member) -> None:
        self.members[(user, project)] = member

    def context_for(self, request: object) -> SecurityContext:
        if not isinstance(request, Request):
            raise InvalidContext("an unknown request")
        member = self.members.get((request.user, request.project))
        if member is None:
            raise InvalidContext("the caller has no role in this project")
        groups = normalize_list(member.groups)
        attributes = {name: _attribute(value) for name, value in member.attributes.items()}
        fingerprint = permission_fingerprint(
            user=request.user,
            project=request.project,
            role=member.role,
            groups=groups,
            attributes=attributes,
            policy_fingerprint=self.policy_fingerprint,
            mapping_snapshots=self.mapping_snapshots,
        )
        return read_context(
            {
                "kind": "security_context",
                "version": 1,
                "user": request.user,
                "project": request.project,
                "role": member.role,
                "groups": groups,
                "attributes": attributes,
                "permission_fingerprint": fingerprint,
            }
        )
