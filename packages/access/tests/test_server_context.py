"""#440 AC1, AC2, AC5, AC6, AC9, AC10, R2, R6: the server-mode context provider reads the directory
as it is at each request, from one consistent state; a caller without a role gets nothing; the
permission fingerprint is stable for one state and changes with every input; another attribute
source plugs in behind a fixed precedence."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from orvex.access.context import permission_fingerprint, read_context
from orvex.access.directory import statements as sql
from orvex.access.directory.attribute_sources import ProjectAttributes
from orvex.access.directory.attributes import Attributes
from orvex.access.errors import ACCESS_FORBIDDEN, AccessError
from orvex.access.server_context import ServerContextProvider, ServerRequest, audience_of
from orvex.access.storage.store import Transaction

from tests.support.access_directory import World

P = "p1"
POLICIES = "sha256:" + "a" * 64
SNAPSHOTS = {"lake": "7"}


def provider(world: World, **changes: Any) -> ServerContextProvider:  # noqa: ANN401
    return ServerContextProvider(
        world.store,
        world.audit,
        changes.get("policies", POLICIES),
        changes.get("snapshots", SNAPSHOTS),
        changes.get("sources", (ProjectAttributes(),)),
    )


@pytest.fixture
def members(world: World) -> tuple[str, str]:
    builder = world.user("builder")
    world.project_with_builder(P, builder)
    ana = world.user("ana")
    world.directory.add_membership(builder, P, ana, "viewer")
    world.directory.create_group(builder, P, "finance")
    return builder, ana


def context(world: World, user: str, **changes: Any) -> Any:  # noqa: ANN401
    return provider(world, **changes).context_for(ServerRequest(user, P))


def test_a_context_is_the_directory_now(world: World, members: tuple[str, str]) -> None:
    builder, ana = members
    attributes = Attributes(world.store, world.audit)
    first = context(world, ana)
    assert (first.role, first.groups, dict(first.attributes)) == ("viewer", (), {})
    world.directory.add_group_member(builder, P, "finance", ana)
    attributes.set(builder, P, ana, "region", "text", "north")
    second = context(world, ana)
    assert second.groups == ("finance",)
    assert dict(second.attributes) == {"region": {"type": "text", "value": "north"}}
    assert second.permission_fingerprint != first.permission_fingerprint
    world.directory.change_role(builder, P, ana, "author")
    assert context(world, ana).role == "author"
    read_context(second.document())  # every context validates against the contract


def test_no_role_no_context(world: World, members: tuple[str, str]) -> None:
    _, ana = members
    stranger = world.user("stranger")
    world.directory.set_administrator(world.admin, stranger, True)  # the flag grants nothing
    for user in (stranger, "u-nobody"):
        with pytest.raises(AccessError) as caught:
            context(world, user)
        assert caught.value.code == ACCESS_FORBIDDEN
    world.directory.deactivate_user(world.admin, ana)
    with pytest.raises(AccessError):
        context(world, ana)
    assert world.refusals()[-3:] == [ACCESS_FORBIDDEN] * 3
    with pytest.raises(AccessError):
        provider(world).context_for(object())


@pytest.mark.parametrize("value", ['"01"', '"-0"'])
def test_a_planted_attribute_builds_no_context(
    world: World, members: tuple[str, str], value: str
) -> None:
    _, ana = members
    with world.store.transaction() as transaction:
        transaction.run(
            "INSERT INTO attributes (project, user_id, name, type, value, set_at) VALUES "
            "(:p, :u, 'region', 'integer', :v, '2026-11-11T00:00:00.000000Z')",
            p=P,
            u=ana,
            v=value,
        )
    with pytest.raises(AccessError) as caught:
        context(world, ana)
    assert caught.value.code == ACCESS_FORBIDDEN
    assert world.refusals() == [ACCESS_FORBIDDEN]
    assert value not in str(world.events()[-1])


def test_a_concurrent_change_never_mixes_two_states(world: World, members: tuple[str, str]) -> None:
    """One writer flips the role and the groups together, in one transaction; every context a
    reader builds meanwhile is wholly before or wholly after."""
    _, ana = members
    stop = threading.Event()

    def flip(transaction: Transaction, to_author: bool) -> None:
        sql.update_role(transaction, P, ana, "author" if to_author else "viewer")
        if to_author:
            sql.insert_group_member(transaction, P, "finance", ana)
        else:
            sql.delete_group_member(transaction, P, "finance", ana)

    def writer() -> None:
        state = True
        while not stop.is_set():
            with world.store.transaction() as transaction:
                flip(transaction, state)
            state = not state

    thread = threading.Thread(target=writer)
    thread.start()
    seen = set()
    try:
        for _ in range(150):
            found = context(world, ana)
            seen.add((found.role, found.groups))
    finally:
        stop.set()
        thread.join()
    assert seen <= {("viewer", ()), ("author", ("finance",))}


def test_the_fingerprint_is_stable_for_one_state(world: World, members: tuple[str, str]) -> None:
    builder, ana = members
    attributes = Attributes(world.store, world.audit)
    world.directory.create_group(builder, P, "ops")
    for group in ("ops", "finance"):
        world.directory.add_group_member(builder, P, group, ana)
    for name in ("zone", "area"):
        attributes.set(builder, P, ana, name, "text", name)
    one = context(world, ana).permission_fingerprint
    assert context(world, ana).permission_fingerprint == one
    # The same state, built in the other order: the same fingerprint.
    for group in ("ops", "finance"):
        world.directory.remove_group_member(builder, P, group, ana)
    for name in ("zone", "area"):
        attributes.remove(builder, P, ana, name)
    for group in ("finance", "ops"):
        world.directory.add_group_member(builder, P, group, ana)
    for name in ("area", "zone"):
        attributes.set(builder, P, ana, name, "text", name)
    assert context(world, ana).permission_fingerprint == one


GOLDEN = permission_fingerprint(
    user="u-ana",
    project="p1",
    role="viewer",
    groups=["finance"],
    attributes={"region": {"type": "text", "value": "north"}},
    policy_fingerprint=POLICIES,
    mapping_snapshots={"lake": "7"},
)


def test_the_golden_vector_pins_the_byte_form() -> None:
    """Computed once from the contract's byte form; any change to that form changes this."""
    assert GOLDEN == "sha256:ccab15bd7eb5bd8cb32743818f4df129cf6c1f1b5bf338673d4d776995d918f5"


@pytest.mark.parametrize(
    "change",
    [
        {"user": "u-ben"},
        {"project": "p2"},
        {"role": "author"},
        {"groups": ["finance", "ops"]},
        {"groups": ["Finance"]},
        {"attributes": {"zone": {"type": "text", "value": "north"}}},
        {"attributes": {"region": {"type": "text", "value": "south"}}},
        {"attributes": {"region": {"type": "list", "values": ["north"]}}},
        {"policy_fingerprint": "sha256:" + "b" * 64},
        {"mapping_snapshots": {"lake": "8"}},
        {"mapping_snapshots": {"lake": "7", "other": "1"}},
    ],
)
def test_every_input_changes_the_fingerprint(change: dict[str, Any]) -> None:
    inputs: dict[str, Any] = {
        "user": "u-ana",
        "project": "p1",
        "role": "viewer",
        "groups": ["finance"],
        "attributes": {"region": {"type": "text", "value": "north"}},
        "policy_fingerprint": POLICIES,
        "mapping_snapshots": {"lake": "7"},
    }
    assert permission_fingerprint(**{**inputs, **change}) != GOLDEN


def test_the_policies_and_snapshots_the_server_pinned_are_in_it(
    world: World, members: tuple[str, str]
) -> None:
    _, ana = members
    base = context(world, ana).permission_fingerprint
    assert context(world, ana, policies="sha256:" + "c" * 64).permission_fingerprint != base
    assert context(world, ana, snapshots={"lake": "8"}).permission_fingerprint != base


class Claims:
    """A second source, as an identity provider's claims will be."""

    def attributes_for(self, transaction: Transaction, user: str, project: str) -> dict[str, Any]:
        return {
            "region": {"type": "text", "value": "from-claims"},
            "badge": {"type": "boolean", "value": True},
        }


class Planted:
    """A later source that hands over something no Builder could set."""

    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes

    def attributes_for(self, transaction: Transaction, user: str, project: str) -> dict[str, Any]:
        return self.attributes


@pytest.mark.parametrize(
    "attributes",
    [
        {"user_id": {"type": "text", "value": "someone-else"}},  # reserved
        {"Region": {"type": "text", "value": "north"}},  # outside the pattern
        {"level": {"type": "integer", "value": "-0"}},
        {"level": {"type": "integer", "value": "9223372036854775808"}},  # past int64
        {"zones": {"type": "list", "values": ["b", "a"]}},  # not in its contract form
        {"badge": {"type": "boolean", "value": "true"}},
        {"badge": {"type": "boolean", "value": True, "extra": 1}},
        {"badge": "true"},
        {f"a{n}": {"type": "boolean", "value": True} for n in range(65)},
    ],
    ids=["reserved", "name", "minus-zero", "int64", "unsorted", "string", "extra", "bare", "65"],
)
def test_every_source_is_checked_like_a_hand_set_value(
    world: World, members: tuple[str, str], attributes: dict[str, Any]
) -> None:
    _, ana = members
    with pytest.raises(AccessError) as caught:
        context(world, ana, sources=(ProjectAttributes(), Planted(attributes)))
    assert caught.value.code == ACCESS_FORBIDDEN
    assert world.refusals() == [ACCESS_FORBIDDEN]


def test_a_document_the_contract_refuses_builds_no_context(
    world: World, members: tuple[str, str]
) -> None:
    _, ana = members
    with pytest.raises(AccessError) as caught:
        context(world, ana, policies="sha256:short")  # `InvalidContext` from the fingerprint
    assert caught.value.code == ACCESS_FORBIDDEN
    assert world.refusals() == [ACCESS_FORBIDDEN]


def test_another_source_plugs_in_and_a_hand_set_value_wins(
    world: World, members: tuple[str, str]
) -> None:
    builder, ana = members
    Attributes(world.store, world.audit).set(builder, P, ana, "region", "text", "by-hand")
    found = context(world, ana, sources=(ProjectAttributes(), Claims()))
    assert dict(found.attributes) == {
        "region": {"type": "text", "value": "by-hand"},
        "badge": {"type": "boolean", "value": True},
    }


def test_the_audience_is_the_callers_role(world: World, members: tuple[str, str]) -> None:
    builder, ana = members
    assert audience_of(world.store, ServerRequest(builder, P)) == "builder"
    assert audience_of(world.store, ServerRequest(ana, P)) == "other"
    assert audience_of(world.store, ServerRequest(world.admin, P)) == "other"
