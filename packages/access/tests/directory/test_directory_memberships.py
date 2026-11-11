"""#436: project memberships on both stores: explicit roles, the first Builder, the last
Builder, no escalation across projects or onto oneself, and revocation."""

from __future__ import annotations

from functools import partial

import pytest
from orvex.access.errors import ACCESS_FORBIDDEN, LAST_BUILDER, VALIDATION_FAILED, AccessError

from tests.support.access_directory import World


def version(world: World, user_id: str) -> int:
    found = world.directory.user(user_id)
    assert found is not None
    return found.authz_version


def test_a_user_without_a_membership_has_no_role(world: World) -> None:
    ana = world.user("ana")
    assert world.directory.role_of(ana, "p1") is None
    assert world.directory.role_of(world.admin, "p1") is None  # the flag grants no data access
    assert world.directory.groups_of(ana, "p1") == frozenset()


def test_an_administrator_assigns_only_the_first_builder(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(world.admin, "p1", ana, "author")
    assert refused.value.code == ACCESS_FORBIDDEN  # the first membership must be a Builder
    world.project_with_builder("p1", builder)
    assert world.directory.role_of(builder, "p1") == "builder"
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(world.admin, "p1", ana, "builder")
    assert refused.value.code == ACCESS_FORBIDDEN  # p1 has a Builder: memberships are theirs


def test_the_builder_manages_the_project_memberships(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "viewer")
    world.directory.change_role(builder, "p1", ana, "builder")
    assert world.directory.role_of(ana, "p1") == "builder"
    world.directory.change_role(ana, "p1", builder, "author")  # another Builder remains: ana
    assert world.directory.role_of(builder, "p1") == "author"
    world.directory.remove_membership(ana, "p1", builder)
    assert world.directory.role_of(builder, "p1") is None


def test_an_author_or_viewer_cannot_manage_memberships(world: World) -> None:
    builder, ana, ben = world.user("builder"), world.user("ana"), world.user("ben")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "author")
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(ana, "p1", ben, "viewer")
    assert refused.value.code == ACCESS_FORBIDDEN


def test_a_builder_cannot_act_in_another_project(world: World) -> None:
    builder, other, ana = world.user("builder"), world.user("other"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.project_with_builder("p2", other)
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(builder, "p2", ana, "builder")
    assert refused.value.code == ACCESS_FORBIDDEN
    with pytest.raises(AccessError) as refused:
        world.directory.remove_membership(builder, "p2", other)
    assert refused.value.code == ACCESS_FORBIDDEN


def test_a_user_cannot_change_their_own_role(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "builder")
    for attempt in (
        partial(world.directory.change_role, ana, "p1", ana, "viewer"),
        partial(world.directory.remove_membership, ana, "p1", ana),
    ):
        with pytest.raises(AccessError) as refused:
            attempt()
        assert refused.value.code == ACCESS_FORBIDDEN


def test_r2_deactivating_the_last_active_builder_is_refused(world: World) -> None:
    builder = world.user("builder")
    world.project_with_builder("p1", builder)
    with pytest.raises(AccessError) as refused:
        world.directory.deactivate_user(world.admin, builder)
    assert refused.value.code == LAST_BUILDER
    assert world.directory.role_of(builder, "p1") == "builder"  # nothing changed
    assert world.refusals() == [LAST_BUILDER]


def test_r2_the_last_active_builder_cannot_remove_or_demote_themselves(world: World) -> None:
    builder = world.user("builder")
    world.project_with_builder("p1", builder)
    for attempt in (
        partial(world.directory.remove_membership, builder, "p1", builder),
        partial(world.directory.change_role, builder, "p1", builder, "author"),
    ):
        with pytest.raises(AccessError) as refused:
            attempt()
        assert refused.value.code == LAST_BUILDER
    assert world.directory.role_of(builder, "p1") == "builder"


def test_a_deactivated_builder_does_not_count_as_one(world: World) -> None:
    first, second = world.user("first"), world.user("second")
    world.project_with_builder("p1", first)
    world.directory.add_membership(first, "p1", second, "builder")
    world.directory.deactivate_user(world.admin, second)  # first remains active
    with pytest.raises(AccessError) as refused:
        world.directory.deactivate_user(world.admin, first)
    assert refused.value.code == LAST_BUILDER
    world.directory.remove_membership(first, "p1", second)  # removing a deactivated Builder
    assert world.directory.role_of(first, "p1") == "builder"


def test_a_deactivated_user_cannot_be_given_a_membership(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.directory.deactivate_user(world.admin, ana)
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(world.admin, "p1", ana, "builder")
    assert refused.value.code == VALIDATION_FAILED
    world.project_with_builder("p1", builder)


def test_one_role_per_project(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "viewer")
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(builder, "p1", ana, "author")
    assert refused.value.code == VALIDATION_FAILED
    assert world.directory.role_of(ana, "p1") == "viewer"


@pytest.mark.parametrize("role", ["owner", "Builder", "", "admin"])
def test_only_the_three_roles_exist(world: World, role: str) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    with pytest.raises(AccessError) as refused:
        world.directory.add_membership(builder, "p1", ana, role)
    assert refused.value.code == VALIDATION_FAILED


def test_revocation_every_change_is_seen_at_once_and_bumps_the_version(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    seen = [version(world, ana)]
    world.directory.add_membership(builder, "p1", ana, "author")
    assert world.directory.role_of(ana, "p1") == "author"
    seen.append(version(world, ana))
    world.directory.change_role(builder, "p1", ana, "viewer")
    assert world.directory.role_of(ana, "p1") == "viewer"
    seen.append(version(world, ana))
    world.directory.remove_membership(builder, "p1", ana)
    assert world.directory.role_of(ana, "p1") is None
    seen.append(version(world, ana))
    assert seen == sorted(set(seen))
    assert len(seen) == 4
