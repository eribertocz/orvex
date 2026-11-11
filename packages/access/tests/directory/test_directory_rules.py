"""#436: the directory's rules alone (no store): who may change what, and the last-Builder and
last-administrator invariants, including the codes only a concurrent change can reach."""

from __future__ import annotations

import pytest
from orvex.access.directory import rules
from orvex.access.directory.records import Role
from orvex.access.directory.rules import Caller
from orvex.access.errors import ACCESS_FORBIDDEN, LAST_ADMINISTRATOR, LAST_BUILDER

ADMIN = Caller("root", active=True, administrator=True, role=None)
BUILDER = Caller("bea", active=True, administrator=False, role="builder")
AUTHOR = Caller("ann", active=True, administrator=False, role="author")
GONE = Caller("old", active=False, administrator=True, role="builder")


def test_only_an_active_administrator_manages_users() -> None:
    assert rules.manage_users(ADMIN, "u1") is None
    assert rules.manage_users(BUILDER, "u1") == ACCESS_FORBIDDEN
    assert rules.manage_users(GONE, "u1") == ACCESS_FORBIDDEN
    assert rules.manage_users(ADMIN, "root") == ACCESS_FORBIDDEN  # never oneself


@pytest.mark.parametrize(
    ("caller", "adding", "role", "has_builder", "expected"),
    [
        (BUILDER, True, "viewer", True, None),
        (BUILDER, False, None, True, None),
        (AUTHOR, True, "viewer", True, ACCESS_FORBIDDEN),
        (ADMIN, True, "builder", False, None),  # the project's first Builder
        (ADMIN, True, "author", False, ACCESS_FORBIDDEN),
        (ADMIN, True, "builder", True, ACCESS_FORBIDDEN),
        (ADMIN, False, None, False, ACCESS_FORBIDDEN),
        (GONE, True, "viewer", True, ACCESS_FORBIDDEN),
    ],
)
def test_memberships(
    caller: Caller, adding: bool, role: Role | None, has_builder: bool, expected: str | None
) -> None:
    found = rules.manage_membership(
        caller, "u1", adding=adding, role=role, project_has_builder=has_builder
    )
    assert found == expected


def test_no_one_changes_their_own_membership() -> None:
    found = rules.manage_membership(
        BUILDER, "bea", adding=False, role="viewer", project_has_builder=True
    )
    assert found == ACCESS_FORBIDDEN


def test_groups_are_the_builders() -> None:
    assert rules.manage_groups(BUILDER, "u1") is None
    assert rules.manage_groups(AUTHOR, "u1") == ACCESS_FORBIDDEN
    assert rules.manage_groups(ADMIN) == ACCESS_FORBIDDEN
    assert rules.manage_groups(BUILDER, "bea") == ACCESS_FORBIDDEN


def test_r2_and_r5_the_invariants() -> None:
    assert rules.keeps_a_builder(0) == LAST_BUILDER
    assert rules.keeps_a_builder(1) is None
    assert rules.keeps_an_administrator(0) == LAST_ADMINISTRATOR
    assert rules.keeps_an_administrator(1) is None
