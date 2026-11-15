"""Security contexts for the repository's conformance tests, built by the access contract's
fixture contexts (`orvex.contracts.access`): the members a test sets, never the module's
internals."""

from __future__ import annotations

import functools
import json
from typing import Any

from orvex.contracts.access.client import AccessClient, system
from orvex.contracts.access.values import ContextFixture, ContextMember
from orvex.server.assembly import access_bindings


@functools.cache
def contexts() -> AccessClient:
    """The access contract, for contexts that need no instance."""
    return AccessClient(*access_bindings(), system("repository-tests"))


def member(
    user: str,
    role: str,
    groups: tuple[str, ...] = (),
    *,
    project: str = "p1",
    instance_admin: bool = False,
    **attributes: str | bool | int | tuple[str, ...],
) -> ContextMember:
    return ContextMember(
        user, project, role, groups, tuple(sorted(attributes.items())), instance_admin
    )


def fixture(policies: str, mapping: dict[str, str], *members: ContextMember) -> ContextFixture:
    return ContextFixture(policies, mapping, members)


def context(found: ContextFixture, user: str, project: str = "p1") -> dict[str, Any]:
    """The caller's `security-context` v1 document, built now from `found`."""
    document: dict[str, Any] = json.loads(contexts().fixture_context(found, user, project))
    return document


def may_reuse(stored: str, found: ContextFixture, user: str, project: str = "p1") -> bool:
    return contexts().fixture_may_reuse(stored, found, user, project)
