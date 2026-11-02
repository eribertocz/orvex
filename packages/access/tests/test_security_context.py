"""The security context and the permission fingerprint (P2-09, #306)."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from orvex.access.context import (
    InvalidContext,
    may_reuse,
    normalize_list,
    permission_fingerprint,
    read_context,
)
from orvex.access.fixtures import FixtureContextProvider, Member, Request

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "contracts/security-context/v1/examples"
POLICIES = "sha256:" + "a" * 64
SNAPSHOTS = {"security.user_zone": "41"}
GOLDEN = ROOT / "tests/golden/security-context-fingerprint.sha256"


def provider() -> FixtureContextProvider:
    found = FixtureContextProvider(POLICIES, SNAPSHOTS)
    found.add("u-7", "p1", Member("viewer", ["collections"], {"zone_id": "7"}))
    return found


def context(**member: Any) -> dict[str, Any]:  # noqa: ANN401
    found = provider()
    found.add(
        "u-7",
        "p1",
        Member(
            **{
                "role": "viewer",
                "groups": ["collections"],
                "attributes": {"zone_id": "7"},
                **member,
            }
        ),
    )
    return found.context_for(Request("u-7", "p1")).document()


def refused(document: dict[str, Any]) -> None:
    with pytest.raises(InvalidContext):
        read_context(document)


# --- the contract -------------------------------------------------------------------------------


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/access/src/orvex/access/security-context-v1.json"
    assert (
        packaged.read_bytes() == (ROOT / "contracts/security-context/v1/schema.json").read_bytes()
    )


@pytest.mark.parametrize("path", sorted((EXAMPLES / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_valid_examples_read(path: Path) -> None:
    read_context(json.loads(path.read_text("utf-8")))


@pytest.mark.parametrize(
    "path", sorted((EXAMPLES / "invalid").glob("*.json")), ids=lambda p: p.stem
)
def test_invalid_examples_are_refused(path: Path) -> None:
    refused(json.loads(path.read_text("utf-8")))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["attributes"].__setitem__("zone_id", None),
        lambda d: d["attributes"].__setitem__("zone_id", {"type": "text", "value": ""}),
        lambda d: d["attributes"].__setitem__("rate", {"type": "decimal", "value": "0.5"}),
        lambda d: d["attributes"].__setitem__("rate", 0.5),
        lambda d: d.__setitem__("groups", ["b", "a"]),
        lambda d: d.__setitem__("groups", ["a", "a"]),
        lambda d: d.__setitem__("is_admin", True),
        lambda d: d.__setitem__("user", "ana@example.com"),
        lambda d: d["attributes"].__setitem__(
            "zones", {"type": "list", "values": [str(i) for i in range(1001)]}
        ),
        lambda d: d["attributes"].__setitem__("zones", {"type": "list", "values": ["b", "a"]}),
        lambda d: d.__setitem__("user", "u-7\n"),
        lambda d: d.__setitem__("project", "p1\n"),
        lambda d: d["attributes"].__setitem__("zone_id", {"type": "integer", "value": "7\n"}),
        lambda d: d["attributes"].__setitem__("zone_id\n", {"type": "text", "value": "7"}),
        lambda d: d.__setitem__("permission_fingerprint", d["permission_fingerprint"] + "\n"),
    ],
)
def test_contexts_outside_the_contract_are_refused(change: Any) -> None:  # noqa: ANN401
    document = context()
    change(document)
    refused(document)


# --- normalization, then validation ------------------------------------------------------------


def test_the_provider_normalizes_lists_once() -> None:
    assert context(groups=["b", "a", "a"])["groups"] == ["a", "b"]
    decomposed, composed = "équipo", "équipo"
    assert context(groups=[decomposed, composed])["groups"] == [composed]
    assert context(groups=["Admin", "admin"])["groups"] == ["Admin", "admin"]  # no case folding
    assert context(groups=[" a", "a"])["groups"] == [" a", "a"]  # no trimming
    zones = context(attributes={"zones": ["9", "10", "9"]})["attributes"]["zones"]
    assert zones == {"type": "list", "values": ["10", "9"]}
    assert normalize_list(["b", "a"]) == ["a", "b"]


# --- absent, not null; exact values -------------------------------------------------------------


def test_a_missing_attribute_is_absent() -> None:
    found = read_context(context(attributes={}))
    assert "zone_id" not in found.attributes


def test_attributes_stay_exact_and_typed() -> None:
    attributes = context(attributes={"big": 9007199254740993, "flag": True, "word": "true"})[
        "attributes"
    ]
    assert attributes["big"] == {"type": "integer", "value": "9007199254740993"}
    assert attributes["flag"] == {"type": "boolean", "value": True}
    assert attributes["word"] == {"type": "text", "value": "true"}


# --- no administrator flag --------------------------------------------------------------------


def test_the_instance_administrator_flag_never_reaches_a_context() -> None:
    plain = context()
    admin = context(instance_admin=True)
    assert admin == plain
    assert "admin" not in json.dumps(admin).lower()


# --- the fingerprint ----------------------------------------------------------------------------


def fingerprint_of(**change: Any) -> str:  # noqa: ANN401
    arguments: dict[str, Any] = {
        "user": "u-7",
        "project": "p1",
        "role": "viewer",
        "groups": ["collections"],
        "attributes": {"zone_id": {"type": "text", "value": "7"}},
        "policy_fingerprint": POLICIES,
        "mapping_snapshots": SNAPSHOTS,
    }
    arguments.update(change)
    return permission_fingerprint(**arguments)


def test_the_fingerprint_changes_with_anything_a_policy_can_read() -> None:
    base = fingerprint_of()
    changes: tuple[dict[str, Any], ...] = (
        {"user": "u-8"},
        {"project": "p2"},
        {"role": "author"},
        {"groups": ["auditors", "collections"]},
        {"attributes": {"zone_id": {"type": "text", "value": "8"}}},
        {"attributes": {"zone_id": {"type": "integer", "value": "7"}}},
        {"attributes": {}},
        {"policy_fingerprint": "sha256:" + "b" * 64},
        {"mapping_snapshots": {"security.user_zone": "42"}},
    )
    for change in changes:
        assert fingerprint_of(**change) != base, change


def test_two_users_with_equal_groups_and_attributes_differ() -> None:
    assert fingerprint_of(user="u-1") != fingerprint_of(user="u-2")


def test_reordered_input_gives_the_same_fingerprint() -> None:
    assert (
        context(groups=["b", "a"])["permission_fingerprint"]
        == context(groups=["a", "b"])["permission_fingerprint"]
    )
    assert (
        context(attributes={"zones": ["2", "1"]})["permission_fingerprint"]
        == context(attributes={"zones": ["1", "2"]})["permission_fingerprint"]
    )


def test_the_byte_form_is_unambiguous() -> None:
    # Concatenation can never collide: lengths and type tags separate every field.
    assert fingerprint_of(groups=["ab", "c"]) != fingerprint_of(groups=["a", "bc"])
    assert fingerprint_of(attributes={"x": {"type": "text", "value": "true"}}) != fingerprint_of(
        attributes={"x": {"type": "boolean", "value": True}}
    )


SCRIPT = """
import sys
sys.path.insert(0, {here!r})
from test_security_context import fingerprint_of
print(fingerprint_of())
"""


def test_the_fingerprint_is_equal_across_processes_and_hash_seeds() -> None:
    here = fingerprint_of()
    for seed in ("0", "1", "4242"):
        run = subprocess.run(
            [sys.executable, "-c", SCRIPT.format(here=str(Path(__file__).parent))],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=ROOT,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert run.stdout.strip() == here
    assert GOLDEN.read_text("ascii").strip() == here


# --- current permissions before reuse ------------------------------------------------------------


def test_reuse_needs_the_current_permissions() -> None:
    found = provider()
    request = Request("u-7", "p1")
    stored = found.context_for(request).permission_fingerprint
    assert may_reuse(stored, found, request)
    found.members[("u-7", "p1")].groups = []  # a group removed between two requests
    assert not may_reuse(stored, found, request)
    found = provider()
    found.mapping_snapshots["security.user_zone"] = "42"  # the mapping table advances
    assert not may_reuse(stored, found, request)


@pytest.mark.parametrize("stored", [None, "", "sha256:" + "0" * 64, "sha256:XYZ", 7])
def test_a_stale_or_forged_fingerprint_gets_nothing(stored: object) -> None:
    assert not may_reuse(stored, provider(), Request("u-7", "p1"))


def test_a_caller_without_a_role_gets_nothing() -> None:
    found = provider()
    stored = found.context_for(Request("u-7", "p1")).permission_fingerprint
    assert not may_reuse(stored, found, Request("u-7", "p2"))
    assert not may_reuse(stored, found, "not a request")


def test_a_context_is_immutable() -> None:
    found = read_context(context())
    with pytest.raises(AttributeError):
        found.role = "builder"  # type: ignore[misc]
    with pytest.raises(TypeError):
        found.attributes["zone_id"] = {}  # type: ignore[index]
    document = found.document()
    copy.deepcopy(document)["groups"].append("x")
    assert found.groups == ("collections",)


def test_list_items_are_text_never_converted() -> None:
    found = provider()
    found.add("u-7", "p1", Member("viewer", ["1"], {"zones": [1, "1"]}))
    with pytest.raises(InvalidContext):
        found.context_for(Request("u-7", "p1"))
    found.add("u-7", "p1", Member("viewer", [1]))  # type: ignore[list-item]
    with pytest.raises(InvalidContext):
        found.context_for(Request("u-7", "p1"))
