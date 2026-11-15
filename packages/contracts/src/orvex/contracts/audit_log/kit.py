"""The conformance kit of the `audit-log` interface contract (#486 G1, G3): recording, the hash
chain and its signed checkpoints, tampering found by verification, retention, key rotation and
one idempotency case per operation that changes state.

Tampering is planted where a consumer can reach it through the contract alone: the anchor
file and the signing key file the `Configuration` names, and anchors shipped outside. Run it
like the access kit (`orvex.contracts.access.kit`); it has no skip list.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from orvex.contracts.access.kit_world import KIT_CLOCK, World, call, code_of, world
from orvex.contracts.audit_log.interface import AUDIT_LOG
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient

ACTOR = "kit-auditor"


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def _query(index: int) -> dict[str, str]:
    """The details of one valid `query` event."""
    receipt = f"sha256:{index:064x}"
    return {"receipt": receipt, "cache": "miss", "mode": "aggregate", "stage": "synchronous"}


def _recorded(client: KitClient, count: int = 1) -> World:
    found = world(client)
    for index in range(count):
        call(client, ACTOR, "record", found.opened, "query", _query(index), "allowed", "p1")
    return found


def _events(client: KitClient) -> list[dict[str, Any]]:
    """The events the kit's actor caused (the instance's own, such as its OIDC mapping at
    opening, are left out)."""
    found = world(client)
    documents = [json.loads(event) for event in call(client, ACTOR, "events", found.opened)]
    return [document for document in documents if document["actor"] == ACTOR]


def _all(client: KitClient) -> int:
    return len(call(client, ACTOR, "events", world(client).opened))


def record_and_read(client: KitClient) -> None:
    _recorded(client, 2)
    kinds = [(event["kind"], event["actor"], event["details"]) for event in _events(client)]
    _expect(kinds == [("query", ACTOR, _query(0)), ("query", ACTOR, _query(1))], f"events {kinds}")


def invalid_event_refused(client: KitClient) -> None:
    found = world(client)
    found_code = code_of(client, ACTOR, "record", found.opened, "Not A Kind", {}, "allowed", None)
    _expect(found_code == "AUDIT_EVENT_INVALID", f"code {found_code}")
    _expect(_events(client) == [], "an invalid event was written")


def refusal_recorded(client: KitClient) -> None:
    found = world(client)
    call(client, ACTOR, "refusal", found.opened, "route", "ACCESS_FORBIDDEN", "p1", "u-2")
    (event,) = _events(client)
    _expect(event["kind"] == "access.refused" and event["outcome"] == "refused", f"{event}")
    _expect(
        event["details"] == {"operation": "route", "code": "ACCESS_FORBIDDEN", "target": "u-2"},
        f"details {event['details']}",
    )


def unrecorded_counted(client: KitClient) -> None:
    found = world(client)
    _expect(call(client, ACTOR, "unrecorded", found.opened) == 0, "a fresh log counts nothing")
    _expect(call(client, ACTOR, "count_unrecorded", found.opened) == 1, "counted")
    _expect(call(client, ACTOR, "unrecorded", found.opened) == 1, "kept")


def chain_verifies(client: KitClient) -> None:
    found = _recorded(client, 3)
    every = _all(client)
    _expect(len(_events(client)) == 3, "recorded")
    _expect(call(client, ACTOR, "verify", found.opened, ()) == every, "verified count")
    _expect(call(client, ACTOR, "verify_at_start", found.opened) == every, "at start")


def _anchors(found: World) -> Path:
    return Path(found.configuration.audit_anchors)


def tampered_anchor_found(client: KitClient) -> None:
    found = _recorded(client)
    lines = _anchors(found).read_text("ascii").splitlines()
    _expect(bool(lines), "no checkpoint was anchored")
    anchor = json.loads(lines[-1])
    anchor["hash"] = ("0" if anchor["hash"][0] != "0" else "1") + anchor["hash"][1:]
    _anchors(found).write_text("\n".join([*lines[:-1], json.dumps(anchor)]) + "\n", "ascii")
    found_code = code_of(client, ACTOR, "verify_at_start", found.opened)
    _expect(found_code == "AUDIT_CHECKPOINT_INVALID", f"code {found_code}")


def forged_outside_anchor_found(client: KitClient) -> None:
    found = _recorded(client)
    line = _anchors(found).read_text("ascii").splitlines()[-1]
    forged = json.loads(line)
    forged["signature"] = forged["signature"][::-1]
    code = code_of(client, ACTOR, "verify", found.opened, (json.dumps(forged),))
    _expect(code == "AUDIT_CHECKPOINT_INVALID", f"code {code}")
    _expect(call(client, ACTOR, "verify", found.opened, (line,)) >= 1, "a true anchor")


def replaced_key_refused(client: KitClient) -> None:
    found = _recorded(client)
    call(client, ACTOR, "close", found.opened)
    found.instance = None
    os.remove(found.configuration.audit_key)
    code = code_of(client, ACTOR, "open", found.configuration)
    _expect(code == "AUDIT_KEY_UNSAFE", f"a missing key gave {code}")


def retention_has_a_minimum(client: KitClient) -> None:
    found = _recorded(client)
    code = code_of(client, ACTOR, "retain", found.opened, 1)
    _expect(code == "AUDIT_RETENTION_INVALID", f"code {code}")
    _expect(call(client, ACTOR, "retain", found.opened, 30) is None, "nothing is that old")


def rotation_keeps_the_chain(client: KitClient) -> None:
    found = _recorded(client, 2)
    fingerprint = call(client, ACTOR, "rotate_key", found.opened)
    _expect(fingerprint.startswith("sha256:"), f"fingerprint {fingerprint}")
    _recorded(client)
    _expect(call(client, ACTOR, "verify_at_start", found.opened) == _all(client), "verified")
    _expect(any(event["kind"] == "audit.key_rotated" for event in _events(client)), "rotation")


def unknown_instance_refused(client: KitClient) -> None:
    found = world(client)
    call(client, ACTOR, "close", found.opened)
    closed = found.opened
    found.instance = None
    code = code_of(client, ACTOR, "events", closed)
    _expect(code == "ACCESS_INSTANCE_UNKNOWN", f"code {code}")


# --- idempotency ---------------------------------------------------------------------------


def _count(client: KitClient) -> object:
    return _all(client)


def _record(client: KitClient, envelope: CallEnvelope) -> object:
    found = world(client)
    return client.send("record", envelope, found.opened, "query", _query(9), "allowed", "p1")


def _refusal(client: KitClient, envelope: CallEnvelope) -> object:
    found = world(client)
    return client.send("refusal", envelope, found.opened, "route", "ACCESS_FORBIDDEN", None, None)


def _count_unrecorded(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("count_unrecorded", envelope, world(client).opened)


def _unrecorded(client: KitClient) -> object:
    return call(client, ACTOR, "unrecorded", world(client).opened)


def _open(client: KitClient, envelope: CallEnvelope) -> object:
    found = world(client)
    answer = client.send("open", envelope, found.configuration)
    found.answers.append(answer)
    found.instance = answer
    return answer


def _opened(client: KitClient) -> object:
    return len(set(world(client).answers))


def _close(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("close", envelope, world(client).opened)


def _closed(client: KitClient) -> object:
    return code_of(client, ACTOR, "unrecorded", world(client).opened)


def _retain(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("retain", envelope, world(client).opened, 30)


def _rotate(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("rotate_key", envelope, world(client).opened)


def _rotations(client: KitClient) -> object:
    documents = call(client, ACTOR, "events", world(client).opened)
    return sum(1 for event in documents if json.loads(event)["kind"] == "audit.key_rotated")


def _aged(client: KitClient) -> object:
    """The count of events, after the first observation planted events older than retention."""
    found = world(client)
    if not found.answers:
        _recorded(client, 2)
        KIT_CLOCK.advance(timedelta(days=2))
        _recorded(client)
        KIT_CLOCK.advance(timedelta(days=40))
        _recorded(client)
        found.answers.append("aged")
    return _count(client)


def _case(operation: str, expectation: str, check: object) -> KitCase:
    name = getattr(check, "__name__", "case").replace("_", "-")
    return KitCase(f"{operation.replace('_', '-')}.{name}", operation, expectation, check)  # type: ignore[arg-type]


def _once(operation: str, expectation: str, send: object, observe: object) -> KitCase:
    check = Idempotent(send, observe)  # type: ignore[arg-type]
    return KitCase(f"{operation.replace('_', '-')}.idempotent", operation, expectation, check)


CASES = (
    _case("record", "recorded events read back in order, with their actor", record_and_read),
    _case("record", "an event that breaks audit-event v1 is never written", invalid_event_refused),
    _case("refusal", "a refusal is recorded with its operation, code and target", refusal_recorded),
    _case("unrecorded", "events that could not be recorded are counted", unrecorded_counted),
    _case("count_unrecorded", "a counted failure is kept", unrecorded_counted),
    _case("verify", "the chain verifies, gapless", chain_verifies),
    _case("verify_at_start", "a tampered anchor file is found", tampered_anchor_found),
    _case("verify", "a forged anchor shipped outside is found", forged_outside_anchor_found),
    _case("open", "a missing signing key is never replaced silently", replaced_key_refused),
    _case(
        "retain", "retention has a minimum and removes nothing too young", retention_has_a_minimum
    ),
    _case(
        "rotate_key",
        "a rotation is recorded and the chain still verifies",
        rotation_keeps_the_chain,
    ),
    _case("events", "a closed instance is refused", unknown_instance_refused),
    _case("close", "a closed instance is refused", unknown_instance_refused),
    _once("open", "one key opens one instance", _open, _opened),
    _once("close", "one key closes once", _close, _closed),
    _once("record", "one key records one event", _record, _count),
    _once("refusal", "one key records one refusal", _refusal, _count),
    _once("count_unrecorded", "one key counts once", _count_unrecorded, _unrecorded),
    _once("retain", "one key removes the old events once", _retain, _aged),
    _once("rotate_key", "one key rotates once", _rotate, _rotations),
)

AUDIT_LOG_KIT = Kit(AUDIT_LOG, recorded=20, cases=CASES)
