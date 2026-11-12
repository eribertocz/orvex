"""#440 AC3, AC4, AC5, R5, R6: who may set a member's attributes, the exact values allowed, and a
stored value that fails the checks building no context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import pytest
from orvex.access.directory.attribute_sources import UnreadableAttribute, stored_attributes
from orvex.access.directory.attributes import Attributes
from orvex.access.errors import ACCESS_FORBIDDEN, VALIDATION_FAILED, AccessError

from tests.support.access_directory import World

P = "p1"


@pytest.fixture
def setup(world: World) -> tuple[World, Attributes, str, str]:
    builder = world.user("builder")
    world.project_with_builder(P, builder)
    ana = world.user("ana")
    world.directory.add_membership(builder, P, ana, "viewer")
    return world, Attributes(world.store, world.audit), builder, ana


def test_a_builder_sets_and_removes_a_members_attributes(
    setup: tuple[World, Attributes, str, str],
) -> None:
    world, attributes, builder, ana = setup
    attributes.set(builder, P, ana, "region", "text", "north")
    attributes.set(builder, P, ana, "zones", "list", ["b", "a"])
    attributes.set(builder, P, ana, "level", "integer", "-12")
    attributes.set(builder, P, ana, "auditor", "boolean", False)
    assert attributes.of(ana, P) == {
        "region": {"type": "text", "value": "north"},
        "zones": {"type": "list", "values": ["a", "b"]},
        "level": {"type": "integer", "value": "-12"},
        "auditor": {"type": "boolean", "value": False},
    }
    attributes.remove(builder, P, ana, "region")
    assert "region" not in attributes.of(ana, P)
    kinds = [event["kind"] for event in world.events()]
    assert kinds.count("attribute.set") == 4
    assert kinds[-1] == "attribute.removed"
    first = next(e for e in world.events() if e["kind"] == "attribute.set")
    assert first["details"] == {
        "user": ana,
        "name": "region",
        "type": "text",
    }  # never the value, in clear or hashed
    held = (json.dumps("north"), hashlib.sha256(b'"north"').hexdigest(), "north")
    assert all(text not in json.dumps(event) for event in world.events() for text in held)


def test_who_may_not_change_attributes(setup: tuple[World, Attributes, str, str]) -> None:
    world, attributes, builder, ana = setup
    author = world.user("author")
    world.directory.add_membership(builder, P, author, "author")
    other_builder = world.user("other")
    world.project_with_builder("p2", other_builder)
    outsider = world.user("outsider")
    attempts: list[Callable[[], None]] = [
        lambda: attributes.set(builder, P, builder, "region", "text", "x"),  # their own
        lambda: attributes.set(author, P, ana, "region", "text", "x"),
        lambda: attributes.set(ana, P, author, "region", "text", "x"),
        lambda: attributes.set(world.admin, P, ana, "region", "text", "x"),
        lambda: attributes.set(other_builder, P, ana, "region", "text", "x"),
    ]
    for attempt in attempts:
        with pytest.raises(AccessError) as caught:
            attempt()
        assert caught.value.code == ACCESS_FORBIDDEN
    with pytest.raises(AccessError) as caught:
        attributes.set(builder, P, outsider, "region", "text", "x")  # not a member
    assert caught.value.code == ACCESS_FORBIDDEN
    with pytest.raises(AccessError) as caught:
        attributes.remove(builder, P, outsider, "region")
    assert caught.value.code == ACCESS_FORBIDDEN
    world.directory.deactivate_user(world.admin, ana)
    with pytest.raises(AccessError) as caught:
        attributes.set(builder, P, ana, "region", "text", "x")  # deactivated
    assert caught.value.code == ACCESS_FORBIDDEN
    with pytest.raises(AccessError) as caught:
        attributes.set(author, P, ana, "region", "date", "x")  # refused before the value
    assert caught.value.code == ACCESS_FORBIDDEN
    assert attributes.of(ana, P) == {}
    assert world.refusals() == [ACCESS_FORBIDDEN] * 9


def test_a_membership_takes_its_attributes_with_it(
    setup: tuple[World, Attributes, str, str],
) -> None:
    world, attributes, builder, ana = setup
    attributes.set(builder, P, ana, "region", "text", "north")
    world.directory.remove_membership(builder, P, ana)
    world.directory.add_membership(builder, P, ana, "viewer")
    assert attributes.of(ana, P) == {}


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("text", ""),
        ("text", "x" * 1025),
        ("text", "é"),  # not NFC
        ("boolean", "1"),
        ("boolean", 1),
        ("integer", 1.0),
        ("integer", "01"),
        ("integer", "-0"),
        ("integer", "-01"),
        ("integer", "+1"),
        ("integer", str(2**63)),
        ("integer", 5),
        ("list", []),
        ("list", ["a"] * 1001),
        ("list", ["é", "é"]),  # equal only after NFC: the second is not NFC
        ("list", ["a", "a"]),
        ("list", "a"),
        ("date", "2026-01-01"),
    ],
)
def test_a_value_outside_its_type_is_refused(
    setup: tuple[World, Attributes, str, str], kind: str, value: object
) -> None:
    world, attributes, builder, ana = setup
    with pytest.raises(AccessError) as caught:
        attributes.set(builder, P, ana, "region", kind, value)
    assert caught.value.code == VALIDATION_FAILED
    assert caught.value.details in ({"field": "value"}, {"field": "type"})  # never the value
    assert attributes.of(ana, P) == {}
    assert world.refusals() == [VALIDATION_FAILED]  # audited, like every other refusal


@pytest.mark.parametrize("name", ["user_id", "groups", "role", "Region", "1st", "a-b", "x" * 64])
def test_a_reserved_or_malformed_name_is_refused(
    setup: tuple[World, Attributes, str, str], name: str
) -> None:
    world, attributes, builder, ana = setup
    with pytest.raises(AccessError) as caught:
        attributes.set(builder, P, ana, name, "text", "x")
    assert caught.value.details == {"field": "name"}
    with pytest.raises(AccessError) as caught:
        attributes.remove(builder, P, ana, name)
    assert caught.value.details == {"field": "name"}
    assert world.refusals() == [VALIDATION_FAILED] * 2


def test_at_most_64_attributes(setup: tuple[World, Attributes, str, str]) -> None:
    world, attributes, builder, ana = setup
    for number in range(64):
        attributes.set(builder, P, ana, f"a{number}", "boolean", True)
    attributes.set(builder, P, ana, "a0", "boolean", False)  # replacing is not a 65th
    with pytest.raises(AccessError):
        attributes.set(builder, P, ana, "a64", "boolean", True)
    with pytest.raises(AccessError):
        attributes.remove(builder, P, ana, "a64")  # never set
    assert len(attributes.of(ana, P)) == 64
    assert world.refusals() == [VALIDATION_FAILED] * 2


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("integer", '"01"'),
        ("integer", '"-0"'),
        ("integer", "5"),  # not a JSON string
        ("list", '["b","a"]'),  # not in its sorted form
        ("text", '"e\\u0301"'),
        ("boolean", '"true"'),
        ("text", "not json"),
    ],
)
def test_a_planted_value_is_unreadable_never_repaired(
    setup: tuple[World, Attributes, str, str], kind: str, value: str
) -> None:
    world, _, _, ana = setup
    with world.store.transaction() as transaction:
        transaction.run(
            "INSERT INTO attributes (project, user_id, name, type, value, set_at) "
            "VALUES (:p, :u, 'region', :k, :v, '2026-11-11T00:00:00.000000Z')",
            p=P,
            u=ana,
            k=kind,
            v=value,
        )
    with world.store.transaction(read_only=True) as transaction, pytest.raises(UnreadableAttribute):
        stored_attributes(transaction, ana, P)
