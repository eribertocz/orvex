"""The security context across modules (P2-09, #306): a cursor or a cached result is reused only
under the caller's current permissions, and a missing attribute reaches the policy model as
missing."""

from __future__ import annotations

import base64
import secrets

import pytest
import yaml
from orvex.access.context import current_fingerprint, may_reuse, read_context
from orvex.access.fixtures import FixtureContextProvider, Member, Request
from orvex.semantic import SemanticError
from orvex.semantic.policies import read_policies
from orvex.semantic.result import (
    CursorBinding,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)

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


def binding(provider: FixtureContextProvider, request: Request) -> CursorBinding:
    """The binding of this request, computed anew from the caller's current context."""
    return CursorBinding(QUERY, MODEL, current_fingerprint(provider, request), 50)


def test_a_group_removed_between_two_pages_refuses_the_cursor_and_the_cache() -> None:
    provider = FixtureContextProvider(POLICIES, {"security.user_zone": "41"})
    provider.add("u-7", "p1", Member("viewer", ["collections"], {"zone_id": "7"}))
    request = Request("u-7", "p1")
    form = ServerSideForm()
    cursor = issue_cursor(form, binding(provider, request), CursorState("41", ("a", 1)))
    cached = current_fingerprint(provider, request)
    available = lambda version: SnapshotStatus.AVAILABLE  # noqa: E731
    assert verify_cursor(form, cursor, binding(provider, request), available).position == ("a", 1)
    provider.members[("u-7", "p1")].groups = []
    with pytest.raises(SemanticError) as caught:
        verify_cursor(form, cursor, binding(provider, request), available)
    assert caught.value.code == "PAGE_CURSOR_INVALID"
    assert not may_reuse(cached, provider, request)


def test_a_mapping_table_that_advances_refuses_the_cursor() -> None:
    provider = FixtureContextProvider(POLICIES, {"security.user_zone": "41"})
    provider.add("u-7", "p1", Member("viewer", [], {"zone_id": "7"}))
    request = Request("u-7", "p1")
    form = ServerSideForm()
    cursor = issue_cursor(form, binding(provider, request), CursorState("41", ("a",)))
    provider.mapping_snapshots["security.user_zone"] = "42"
    with pytest.raises(SemanticError):
        verify_cursor(form, cursor, binding(provider, request), lambda v: SnapshotStatus.AVAILABLE)


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
    provider = FixtureContextProvider(policies.fingerprint, {})
    provider.add("u-7", "p1", Member("viewer", [], {}))
    context = read_context(provider.context_for(Request("u-7", "p1")).document())
    assert policies.missing_attributes("own_zone", context.attributes) == {"zone_id"}
    provider.add("u-8", "p1", Member("viewer", [], {"zone_id": "7"}))
    other = provider.context_for(Request("u-8", "p1"))
    assert policies.missing_attributes("own_zone", other.attributes) == frozenset()
