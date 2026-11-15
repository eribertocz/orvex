"""The security context across modules (P2-09, #306): a cursor or a cached result is reused only
under the caller's current permissions, and a missing attribute reaches the policy model as
missing."""

from __future__ import annotations

import base64
import json
import secrets

import pytest
import yaml
from orvex.contracts.access.values import ContextFixture
from orvex.semantic import SemanticError
from orvex.semantic.policies import read_policies
from orvex.semantic.result import (
    CursorBinding,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)

from tests.support.access_contexts import context, contexts, fixture, may_reuse, member
from tests.support.policies import GROUPS, MANAGED_TABLES, bank

POLICIES = "sha256:" + "a" * 64
QUERY = "sha256:" + "1" * 64
MODEL = "sha256:" + "2" * 64


class ServerSideForm:
    def __init__(self) -> None:
        self.states: dict[str, tuple[CursorBinding, CursorState]] = {}

    def issue(self, binding: CursorBinding, state: CursorState) -> str:
        handle = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        self.states[handle] = (binding, state)
        return handle

    def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
        kept = self.states.get(cursor)
        return kept[1] if kept is not None and kept[0] == binding else None


def binding(found: ContextFixture) -> CursorBinding:
    """The binding of u-7's request, computed anew from their current context."""
    return CursorBinding(QUERY, MODEL, context(found, "u-7")["permission_fingerprint"], 50)


def test_a_group_removed_between_two_pages_refuses_the_cursor_and_the_cache() -> None:
    mapping = {"security.user_zone": "41"}
    found = fixture(POLICIES, mapping, member("u-7", "viewer", ("collections",), zone_id="7"))
    form = ServerSideForm()
    cursor = issue_cursor(form, binding(found), CursorState("41", ("a", 1)))
    cached = context(found, "u-7")["permission_fingerprint"]
    available = lambda version: SnapshotStatus.AVAILABLE  # noqa: E731
    assert verify_cursor(form, cursor, binding(found), available).position == ("a", 1)
    removed = fixture(POLICIES, mapping, member("u-7", "viewer", (), zone_id="7"))
    with pytest.raises(SemanticError) as caught:
        verify_cursor(form, cursor, binding(removed), available)
    assert caught.value.code == "PAGE_CURSOR_INVALID"
    assert not may_reuse(cached, removed, "u-7")


def test_a_mapping_table_that_advances_refuses_the_cursor() -> None:
    caller = member("u-7", "viewer", zone_id="7")
    found = fixture(POLICIES, {"security.user_zone": "41"}, caller)
    form = ServerSideForm()
    cursor = issue_cursor(form, binding(found), CursorState("41", ("a",)))
    advanced = fixture(POLICIES, {"security.user_zone": "42"}, caller)
    with pytest.raises(SemanticError):
        verify_cursor(form, cursor, binding(advanced), lambda v: SnapshotStatus.AVAILABLE)


def test_a_missing_attribute_reaches_the_policy_model_as_missing() -> None:
    document = {
        "version": 1,
        "row_policies": [
            {
                "name": "own_zone",
                "dataset": "customers",
                "kind": "predicate",
                "predicate": "zone_id = $zone_id",
            }
        ],
    }
    policies = read_policies(
        yaml.safe_dump(document), bank(), groups=GROUPS, managed_tables=MANAGED_TABLES
    )
    found = fixture(
        policies.fingerprint, {}, member("u-7", "viewer"), member("u-8", "viewer", zone_id="7")
    )
    read = json.loads(contexts().read_context(json.dumps(context(found, "u-7"))))
    assert policies.missing_attributes("own_zone", read["attributes"]) == {"zone_id"}
    other = context(found, "u-8")
    assert policies.missing_attributes("own_zone", other["attributes"]) == frozenset()
