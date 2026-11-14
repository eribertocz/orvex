"""#436: a project's groups on both stores: the Builder manages them, names are NFC, members
are project members, and every change bumps the member's `authz_version`."""

from __future__ import annotations

import unicodedata
from functools import partial

import pytest
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NOT_FOUND, VALIDATION_FAILED, AccessError

from tests.support.access_directory import World


@pytest.fixture
def project(world: World) -> tuple[str, str]:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "viewer")
    world.directory.create_group(builder, "p1", "collections")
    return builder, ana


def version(world: World, user_id: str) -> int:
    found = world.directory.user(user_id)
    assert found is not None
    return found.authz_version


def test_a_member_is_added_and_removed_and_each_change_bumps(
    world: World, project: tuple[str, str]
) -> None:
    builder, ana = project
    before = version(world, ana)
    world.directory.add_group_member(builder, "p1", "collections", ana)
    assert world.directory.groups_of(ana, "p1") == frozenset({"collections"})
    assert world.directory.groups_of(ana, "p2") == frozenset()  # groups are per project
    world.directory.remove_group_member(builder, "p1", "collections", ana)
    assert world.directory.groups_of(ana, "p1") == frozenset()
    assert version(world, ana) == before + 2


def test_only_the_builder_manages_groups(world: World, project: tuple[str, str]) -> None:
    _, ana = project
    for attempt in (
        partial(world.directory.create_group, ana, "p1", "other"),
        partial(world.directory.add_group_member, ana, "p1", "collections", ana),
        partial(world.directory.create_group, world.admin, "p1", "other"),
    ):
        with pytest.raises(AccessError) as refused:
            attempt()
        assert refused.value.code == ACCESS_FORBIDDEN


def test_a_builder_cannot_change_their_own_groups(world: World, project: tuple[str, str]) -> None:
    builder, _ = project
    with pytest.raises(AccessError) as refused:
        world.directory.add_group_member(builder, "p1", "collections", builder)
    assert refused.value.code == ACCESS_FORBIDDEN


def test_a_group_member_must_be_a_project_member(world: World, project: tuple[str, str]) -> None:
    builder, _ = project
    outsider = world.user("outsider")
    with pytest.raises(AccessError) as refused:
        world.directory.add_group_member(builder, "p1", "collections", outsider)
    assert refused.value.code == USER_NOT_FOUND  # not a member: as if no such user


def test_removing_a_membership_removes_its_group_memberships(
    world: World, project: tuple[str, str]
) -> None:
    builder, ana = project
    world.directory.add_group_member(builder, "p1", "collections", ana)
    world.directory.remove_membership(builder, "p1", ana)
    world.directory.add_membership(builder, "p1", ana, "viewer")
    assert world.directory.groups_of(ana, "p1") == frozenset()


def test_deleting_a_group_bumps_every_member(world: World, project: tuple[str, str]) -> None:
    builder, ana = project
    world.directory.add_group_member(builder, "p1", "collections", ana)
    before = version(world, ana)
    world.directory.delete_group(builder, "p1", "collections")
    assert world.directory.groups_of(ana, "p1") == frozenset()
    assert version(world, ana) == before + 1


def test_a_deactivated_user_has_no_groups(world: World, project: tuple[str, str]) -> None:
    builder, ana = project
    world.directory.add_group_member(builder, "p1", "collections", ana)
    world.directory.deactivate_user(world.admin, ana)
    assert world.directory.groups_of(ana, "p1") == frozenset()


@pytest.mark.parametrize(
    "name", ["", "a" * 65, "line\nbreak", unicodedata.normalize("NFD", "cobranza-ñ")]
)
def test_a_group_name_outside_the_shape_is_refused(
    world: World, project: tuple[str, str], name: str
) -> None:
    builder, _ = project
    with pytest.raises(AccessError) as refused:
        world.directory.create_group(builder, "p1", name)
    assert refused.value.code == VALIDATION_FAILED


def test_a_group_name_is_unique_in_its_project(world: World, project: tuple[str, str]) -> None:
    builder, _ = project
    with pytest.raises(AccessError) as refused:
        world.directory.create_group(builder, "p1", "collections")
    assert refused.value.code == VALIDATION_FAILED
