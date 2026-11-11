"""The audit log's hash chain and signatures, and their verification (domain).

- `hash(n) = SHA-256(hash(n-1) || canonical JSON of event n)`, with 32 zero bytes before the
  first event.
- Signed payloads carry a domain line, so a signature made for one purpose is never valid for
  another: the head, a checkpoint, and each direction of a key rotation.
- `verify` reads rows already loaded and raises the first problem it finds, naming the event or
  the checkpoint (`audit-event` v1 README, "What is detected"). A log that no longer starts at
  1 must start right after the newest `audit.retention` event's `last_seq`, and from a signed
  checkpoint at that place: nothing else may remove events.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orvex.access.audit.signing_key import fingerprint, verifies
from orvex.access.errors import AUDIT_CHAIN_BROKEN, AUDIT_CHECKPOINT_INVALID, AccessError

ZERO = "0" * 64


def link(previous: str, document: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + document).hexdigest()


def head_payload(seq: int, hash: str) -> bytes:
    return f"audit-head/v1\n{seq}\n{hash}".encode("ascii")


def checkpoint_payload(seq: int, hash: str, at: str, key: str) -> bytes:
    return f"audit-checkpoint/v1\n{seq}\n{hash}\n{at}\n{key}".encode("ascii")


def next_key_payload(public: bytes) -> bytes:
    """What the previous key signs at a rotation: the new public key."""
    return b"audit-key/v1\nnext\n" + public


def previous_key_payload(public: bytes) -> bytes:
    """What the new key signs at a rotation: the previous public key."""
    return b"audit-key/v1\nprevious\n" + public


@dataclass(frozen=True, slots=True)
class EventRow:
    seq: int
    id: str
    at: str
    kind: str
    document: str
    hash: str


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    seq: int
    hash: str
    at: str
    key: str
    signature: str

    def anchor(self) -> str:
        """The checkpoint as one line of the anchor file."""
        fields = {"seq": self.seq, "hash": self.hash, "at": self.at, "key": self.key}
        return json.dumps(fields | {"signature": self.signature}, sort_keys=True)


@dataclass(frozen=True, slots=True)
class HeadRow:
    seq: int
    hash: str
    key: str | None
    signature: str | None


@dataclass(frozen=True, slots=True)
class KeyRow:
    key: str
    public_key: str
    previous: str | None
    previous_signs: str | None
    signs_previous: str | None


def _broken(event: str, seq: int, reason: str) -> AccessError:
    return AccessError(
        AUDIT_CHAIN_BROKEN,
        "The audit log's chain does not hold.",
        {"event": event, "seq": str(seq), "reason": reason},
    )


def _invalid(seq: int, reason: str) -> AccessError:
    return AccessError(
        AUDIT_CHECKPOINT_INVALID,
        "An audit checkpoint does not hold.",
        {"seq": str(seq), "reason": reason},
    )


def trusted_keys(current: bytes, keys: Sequence[KeyRow]) -> dict[str, bytes]:
    """The keys the current one vouches for: itself, then each predecessor that the key after it
    signed at its rotation. A key nobody vouches for is not trusted, wherever it is stored."""
    by_name = {row.key: row for row in keys}
    trusted = {fingerprint(current): current}
    name, public = fingerprint(current), current
    while (row := by_name.get(name)) is not None and row.previous is not None:
        earlier = by_name.get(row.previous)
        if earlier is None or row.signs_previous is None:
            break
        earlier_public = bytes.fromhex(earlier.public_key)
        if fingerprint(earlier_public) != earlier.key or not verifies(
            public, bytes.fromhex(row.signs_previous), previous_key_payload(earlier_public)
        ):
            break
        trusted[earlier.key] = earlier_public
        name, public = earlier.key, earlier_public
    return trusted


def verify(
    events: Sequence[EventRow],
    checkpoints: Sequence[CheckpointRow],
    head: HeadRow,
    trusted: Mapping[str, bytes],
    anchors: Sequence[str] = (),
) -> int:
    """Check every signature, every link and every anchor; return how many events verified."""
    by_seq: dict[int, CheckpointRow] = {}
    for checkpoint in checkpoints:
        public = trusted.get(checkpoint.key)
        payload = checkpoint_payload(checkpoint.seq, checkpoint.hash, checkpoint.at, checkpoint.key)
        if public is None or not verifies(public, bytes.fromhex(checkpoint.signature), payload):
            raise _invalid(checkpoint.seq, "signature")
        by_seq[checkpoint.seq] = checkpoint
    for line in anchors:
        if not line.strip():
            continue
        try:
            anchored = json.loads(line)
            seq = int(anchored["seq"])
        except (ValueError, KeyError, TypeError) as error:
            raise _invalid(0, "anchor_unreadable") from error
        stored = by_seq.get(seq)
        if stored is None or stored.anchor() != json.dumps(anchored, sort_keys=True):
            raise _invalid(seq, "anchor")
    if events:
        first = events[0].seq
        if first == 1:
            previous = ZERO
        elif first - 1 in by_seq:
            previous = by_seq[first - 1].hash  # retention removed what came before
        else:
            raise _broken(events[0].id, first, "start")
        reached: list[int] = []  # how far each retention removed, read from verified documents
        expected_seq = first
        for event in events:
            if event.seq != expected_seq:
                raise _broken(event.id, event.seq, "gap")
            document = json.loads(event.document)
            if (document.get("id"), document.get("at"), document.get("kind")) != (
                event.id,
                event.at,
                event.kind,
            ):
                raise _broken(event.id, event.seq, "columns")
            if link(previous, event.document.encode("utf-8")) != event.hash:
                raise _broken(event.id, event.seq, "hash")
            if event.kind == "audit.retention":
                last = document.get("details", {}).get("last_seq")
                if not isinstance(last, int) or isinstance(last, bool):
                    raise _broken(event.id, event.seq, "columns")
                reached.append(last)
            covering = by_seq.get(event.seq)
            if covering is not None and covering.hash != event.hash:
                raise _invalid(event.seq, "hash")
            previous, expected_seq = event.hash, event.seq + 1
        if first > 1 and (not reached or max(reached) != first - 1):
            # Only retention removes events, and it records how far it went in the chain itself:
            # a start no signed retention event explains is a removal by someone else.
            raise _broken(events[0].id, first, "start")
        last_seq, last_hash = events[-1].seq, events[-1].hash
    else:
        if head.seq > 0:
            # Retention always records its own event after removing: an empty log whose head has
            # moved was emptied by someone else.
            raise _broken("", head.seq, "start")
        last_seq, last_hash = (max(by_seq), by_seq[max(by_seq)].hash) if by_seq else (0, ZERO)
    if max(by_seq, default=0) > last_seq:
        raise _invalid(max(by_seq), "beyond_head")
    if (head.seq, head.hash) != (last_seq, last_hash):
        raise _invalid(head.seq, "head")
    if head.seq:
        public = trusted.get(head.key or "")
        signature = bytes.fromhex(head.signature or "")
        if public is None or not verifies(public, signature, head_payload(head.seq, head.hash)):
            raise _invalid(head.seq, "head_signature")
    return len(events)
