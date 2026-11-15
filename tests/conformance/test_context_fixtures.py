"""The fixture provider builds the contexts the P2-06 to P2-08 tests use (P2-09, #306).

Those package tests carry only opaque permission fingerprints, because a `semantic` test may not
import `orvex.access`. Here, at the repository level, each one is built through the access
contract's fixture contexts instead, and the semantic side accepts it unchanged."""

from __future__ import annotations

import json

import yaml
from orvex.contracts.access.values import ContextFixture
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt import verify_receipt
from orvex.semantic.result import (
    CursorBinding,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)

from tests.conformance.test_security_context_reuse import ServerSideForm
from tests.support.access_contexts import context, fixture, may_reuse, member
from tests.support.policies import GROUPS, MANAGED_TABLES, bank
from tests.support.receipts import SEAL, SampleViewer, receipt

OWN_ZONE = "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"


def provider() -> tuple[ContextFixture, PolicyModel]:
    document = {
        "version": 1,
        "row_policies": [
            {
                "name": "own_zone_only",
                "dataset": "customers",
                "kind": "predicate",
                "predicate": OWN_ZONE,
            }
        ],
        "column_policies": [
            {
                "member": "customers.national_id",
                "default": "hidden",
                "visible_to_groups": ["collections"],
            }
        ],
    }
    policies = read_policies(
        yaml.safe_dump(document), bank(), groups=GROUPS, managed_tables=MANAGED_TABLES
    )
    found = fixture(
        policies.fingerprint,
        {"security.user_zone": "41"},
        member("analyst", "viewer", ("analysts",), user_id="analyst"),
        member("collector", "author", ("collections",), user_id="collector", zone_id="7"),
        member("builder", "builder", ("management",), user_id="builder", instance_admin=True),
    )
    return found, policies


def test_p2_06_cursor_bindings_come_from_provider_contexts() -> None:
    found, _ = provider()
    form = ServerSideForm()
    for user in ("analyst", "collector", "builder"):
        fingerprint = context(found, user)["permission_fingerprint"]
        binding = CursorBinding("sha256:" + "1" * 64, "sha256:" + "2" * 64, fingerprint, 50)
        cursor = issue_cursor(form, binding, CursorState("41", (user,)))
        state = verify_cursor(form, cursor, binding, lambda v: SnapshotStatus.AVAILABLE)
        assert state.position == (user,)


def test_p2_07_receipts_record_a_provider_fingerprint() -> None:
    found, _ = provider()
    fingerprint = context(found, "collector")["permission_fingerprint"]
    record = receipt(permission_fingerprint=fingerprint)
    assert record.document()["security"]["permission_fingerprint"] == fingerprint
    viewer = SampleViewer()
    assert verify_receipt(record.view(viewer), record, SEAL, viewer)


def test_p2_08_policies_read_provider_contexts() -> None:
    found, policies = provider()
    expected: dict[str, frozenset[str]] = {"analyst": frozenset(), "collector": frozenset()}
    for user, missing in expected.items():
        built = context(found, user)
        assert set(built["groups"]) <= GROUPS
        assert policies.missing_attributes("own_zone_only", built["attributes"]) == missing
    builder = context(found, "builder")
    assert "instance_admin" not in json.dumps(builder)
    hidden = next(iter(policies.column_policies))
    assert (
        policies.protection(hidden, tuple(builder["groups"])) == "hidden"
    )  # no implicit exemption (#238)


def test_p2_10_a_catalog_follows_the_current_context_never_a_stale_one() -> None:
    from orvex.semantic.catalog import catalog_for_context

    found, policies = provider()
    stored = context(found, "collector")
    shown = catalog_for_context(bank(), policies, stored)
    assert "national_id" in json.dumps(shown)
    changed = fixture(
        found.policy_fingerprint,
        found.mapping_snapshots,
        member("collector", "author", ("analysts",), user_id="collector"),
    )
    assert not may_reuse(stored["permission_fingerprint"], changed, "collector")  # refused
    fresh = catalog_for_context(bank(), policies, context(changed, "collector"))
    assert "national_id" not in json.dumps(fresh)
