"""#436: users and the administrator flag, on both stores: who may change them, unique names,
deactivation, the last administrator, and revocation through `authz_version`."""

from __future__ import annotations

from functools import partial

import pytest
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    USER_NAME_TAKEN,
    VALIDATION_FAILED,
    AccessError,
)

from tests.support.access_directory import World


def code_of(error: pytest.ExceptionInfo[AccessError]) -> str:
    return error.value.code


def test_an_administrator_creates_an_active_user_with_a_random_id(world: World) -> None:
    created = world.directory.create_user(world.admin, "ana", "Ana Pérez")
    assert created.active
    assert not created.administrator
    assert created.user_name == "ana"
    assert created.display_name == "Ana Pérez"
    assert created.authz_version == 1
    assert len(created.id) == 22
    assert created.id != world.admin
    assert world.directory.user(created.id) == created


def test_r1_a_non_administrator_cannot_create_a_user(world: World) -> None:
    ana = world.user("ana")
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(ana, "ben", "Ben")
    assert code_of(refused) == ACCESS_FORBIDDEN
    assert world.refusals() == [ACCESS_FORBIDDEN]  # recorded by the hook (#437 replaces it)


def test_an_unknown_caller_may_do_nothing(world: World) -> None:
    with pytest.raises(AccessError) as refused:
        world.directory.create_user("no-such-user", "ben", "Ben")
    assert code_of(refused) == ACCESS_FORBIDDEN


@pytest.mark.parametrize("taken", ["ana", "ANA", "Ana"])
def test_r3_user_names_are_unique_without_regard_to_case(world: World, taken: str) -> None:
    world.user("ana")
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(world.admin, taken, "Other")
    assert code_of(refused) == USER_NAME_TAKEN


@pytest.mark.parametrize(
    "name", ["", "ana maría", "\u0430na", "-ana", "a" * 129, "ana\n", "ana/../b"]
)
def test_a_user_name_outside_the_shape_is_refused(world: World, name: str) -> None:
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(world.admin, name, "Someone")
    assert code_of(refused) == VALIDATION_FAILED


def test_ids_are_never_reused(world: World) -> None:
    ids = {world.user(f"user{index}") for index in range(50)}
    assert len(ids) == 50
    assert world.admin not in ids


def test_deactivation_stops_access_at_once_and_keeps_memberships(world: World) -> None:
    builder, ana = world.user("builder"), world.user("ana")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", ana, "author")
    before = world.directory.user(ana)
    world.directory.deactivate_user(world.admin, ana)
    assert world.directory.role_of(ana, "p1") is None
    after = world.directory.user(ana)
    assert after is not None
    assert before is not None
    assert after.state == "deactivated"
    assert after.authz_version > before.authz_version
    world.directory.reactivate_user(world.admin, ana)
    assert world.directory.role_of(ana, "p1") == "author"  # the membership stayed


def test_a_deactivated_caller_may_do_nothing(world: World) -> None:
    other = world.user("other")
    world.directory.set_administrator(world.admin, other, True)
    world.directory.deactivate_user(world.admin, other)
    with pytest.raises(AccessError) as refused:
        world.directory.create_user(other, "ben", "Ben")
    assert code_of(refused) == ACCESS_FORBIDDEN


def test_only_an_administrator_grants_the_flag_and_never_to_themselves(world: World) -> None:
    ana, ben = world.user("ana"), world.user("ben")
    with pytest.raises(AccessError) as refused:
        world.directory.set_administrator(ana, ben, True)
    assert code_of(refused) == ACCESS_FORBIDDEN
    with pytest.raises(AccessError) as refused:
        world.directory.set_administrator(ana, ana, True)
    assert code_of(refused) == ACCESS_FORBIDDEN
    world.directory.set_administrator(world.admin, ana, True)
    granted = world.directory.user(ana)
    assert granted is not None
    assert granted.administrator
    assert granted.authz_version == 2


def test_no_caller_changes_their_own_flag_or_state(world: World) -> None:
    second = world.user("second")
    world.directory.set_administrator(world.admin, second, True)
    for attempt in (
        partial(world.directory.set_administrator, second, second, False),
        partial(world.directory.deactivate_user, second, second),
    ):
        with pytest.raises(AccessError) as refused:
            attempt()
        assert code_of(refused) == ACCESS_FORBIDDEN


def test_a_change_to_what_a_user_may_do_bumps_their_authz_version(world: World) -> None:
    ana = world.user("ana")
    versions = []
    for change in (
        partial(world.directory.set_administrator, world.admin, ana, True),
        partial(world.directory.set_administrator, world.admin, ana, False),
        partial(world.directory.deactivate_user, world.admin, ana),
        partial(world.directory.reactivate_user, world.admin, ana),
    ):
        change()
        found = world.directory.user(ana)
        assert found is not None
        versions.append(found.authz_version)
    assert versions == [2, 3, 4, 5]


def test_r5_the_last_administrator_cannot_be_removed(world: World) -> None:
    """No path removes the one active administrator: they cannot touch their own flag or state,
    and nobody else may. A concurrent removal is refused too
    (`test_directory_concurrency.py`)."""
    for attempt in (
        partial(world.directory.set_administrator, world.admin, world.admin, False),
        partial(world.directory.deactivate_user, world.admin, world.admin),
    ):
        with pytest.raises(AccessError):
            attempt()
    found = world.directory.user(world.admin)
    assert found is not None
    assert found.administrator
    assert found.active
