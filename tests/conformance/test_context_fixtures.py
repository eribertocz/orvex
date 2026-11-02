"""The fixture provider builds the contexts the P2-06 to P2-08 tests use (P2-09, #306).

Those package tests carry only opaque permission fingerprints, because a `semantic` test may not
import `orvex.access`. Here, at the repository level, each one is built through the provider
instead, and the semantic side accepts it unchanged."""

from __future__ import annotations

import yaml
from orvex.access.fixtures import FixtureContextProvider, Member, Request
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
from tests.support.policies import GROUPS, MANAGED_TABLES, bank
from tests.support.receipts import SEAL, SampleViewer, receipt

OWN_ZONE = "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"


def provider() -> tuple[FixtureContextProvider, PolicyModel]:
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
    found = FixtureContextProvider(policies.fingerprint, {"security.user_zone": "41"})
    found.add("analyst", "p1", Member("viewer", ["analysts"], {"user_id": "analyst"}))
    found.add(
        "collector",
        "p1",
        Member("author", ["collections"], {"user_id": "collector", "zone_id": "7"}),
    )
    found.add(
        "builder",
        "p1",
        Member("builder", ["management"], {"user_id": "builder"}, instance_admin=True),
    )
    return found, policies


def test_p2_06_cursor_bindings_come_from_provider_contexts() -> None:
    found, _ = provider()
    form = ServerSideForm()
    for user in ("analyst", "collector", "builder"):
        fingerprint = found.context_for(Request(user, "p1")).permission_fingerprint
        binding = CursorBinding("sha256:" + "1" * 64, "sha256:" + "2" * 64, fingerprint, 50)
        cursor = issue_cursor(form, binding, CursorState("41", (user,)))
        state = verify_cursor(form, cursor, binding, lambda v: SnapshotStatus.AVAILABLE)
        assert state.position == (user,)


def test_p2_07_receipts_record_a_provider_fingerprint() -> None:
    found, _ = provider()
    fingerprint = found.context_for(Request("collector", "p1")).permission_fingerprint
    record = receipt(permission_fingerprint=fingerprint)
    assert record.document()["security"]["permission_fingerprint"] == fingerprint
    viewer = SampleViewer()
    assert verify_receipt(record.view(viewer), record, SEAL, viewer)


def test_p2_08_policies_read_provider_contexts() -> None:
    found, policies = provider()
    expected: dict[str, frozenset[str]] = {"analyst": frozenset(), "collector": frozenset()}
    for user, missing in expected.items():
        context = found.context_for(Request(user, "p1"))
        assert set(context.groups) <= GROUPS
        assert policies.missing_attributes("own_zone_only", context.attributes) == missing
    builder = found.context_for(Request("builder", "p1"))
    hidden = next(iter(policies.column_policies))
    assert policies.protection(hidden, builder.groups) == "hidden"  # no implicit exemption (#238)
