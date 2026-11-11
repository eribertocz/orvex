"""#437: `chain.verify` and key trust alone, on rows built here (no store): every planted shape
is named, and only keys the current one vouches for are trusted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from orvex.access.audit import chain
from orvex.access.audit import signing_key as keys
from orvex.access.errors import AUDIT_CHAIN_BROKEN, AUDIT_CHECKPOINT_INVALID, AccessError

AT = "2026-11-10T00:00:00.000000Z"


@pytest.fixture
def key(tmp_path: Path) -> keys.SigningKey:
    return keys.create(tmp_path / "k.key")


def event(seq: int, previous: str, kind: str = "auth.logout", **details: object) -> chain.EventRow:
    document = {"id": f"e{seq}", "at": AT, "kind": kind, "details": details}
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return chain.EventRow(seq, f"e{seq}", AT, kind, text, chain.link(previous, text.encode()))


def checkpoint(key: keys.SigningKey, row: chain.EventRow) -> chain.CheckpointRow:
    payload = chain.checkpoint_payload(row.seq, row.hash, AT, key.fingerprint)
    return chain.CheckpointRow(row.seq, row.hash, AT, key.fingerprint, key.sign(payload).hex())


def head(key: keys.SigningKey, row: chain.EventRow) -> chain.HeadRow:
    signature = key.sign(chain.head_payload(row.seq, row.hash)).hex()
    return chain.HeadRow(row.seq, row.hash, key.fingerprint, signature)


def problem(*arguments: object) -> AccessError:
    with pytest.raises(AccessError) as caught:
        chain.verify(*arguments)  # type: ignore[arg-type]
    return caught.value


def test_a_sound_chain_with_blank_anchor_lines_verifies(key: keys.SigningKey) -> None:
    one = event(1, chain.ZERO)
    two = event(2, one.hash)
    point = checkpoint(key, two)
    trusted = {key.fingerprint: key.public}
    lines = ["", point.anchor(), "   "]
    assert chain.verify([one, two], [point], head(key, two), trusted, lines) == 2


@pytest.mark.parametrize("line", ["not json", "[]", '{"no": "seq"}', '{"seq": "x"}'])
def test_an_unreadable_anchor_line_is_named(key: keys.SigningKey, line: str) -> None:
    one = event(1, chain.ZERO)
    found = problem([one], [], head(key, one), {key.fingerprint: key.public}, [line])
    assert found.details == {"seq": "0", "reason": "anchor_unreadable"}


def test_a_retention_event_without_a_whole_number_is_a_broken_column(key: keys.SigningKey) -> None:
    one = event(1, chain.ZERO, "audit.retention", last_seq="8")
    found = problem([one], [], head(key, one), {key.fingerprint: key.public})
    assert found.code == AUDIT_CHAIN_BROKEN
    assert found.details["reason"] == "columns"


def test_a_checkpoint_on_another_hash_is_named(key: keys.SigningKey) -> None:
    one = event(1, chain.ZERO)
    other = event(1, "1" * 64)
    found = problem([one], [checkpoint(key, other)], head(key, one), {key.fingerprint: key.public})
    assert found.details == {"seq": "1", "reason": "hash"}


def test_a_checkpoint_beyond_the_head_is_named(key: keys.SigningKey) -> None:
    one = event(1, chain.ZERO)
    two = event(2, one.hash)
    found = problem([one], [checkpoint(key, two)], head(key, one), {key.fingerprint: key.public})
    assert found.code == AUDIT_CHECKPOINT_INVALID
    assert found.details["reason"] == "beyond_head"


def test_an_empty_new_log_verifies() -> None:
    assert chain.verify([], [], chain.HeadRow(0, chain.ZERO, None, None), {}) == 0


def rotation(old: keys.SigningKey, new: keys.SigningKey, *, vouched: bool) -> list[chain.KeyRow]:
    signer = new if vouched else old  # only the new key may vouch for the old one
    endorsement = signer.sign(chain.previous_key_payload(old.public)).hex()
    return [
        chain.KeyRow(old.fingerprint, old.public.hex(), None, None, None),
        chain.KeyRow(
            new.fingerprint,
            new.public.hex(),
            old.fingerprint,
            old.sign(chain.next_key_payload(new.public)).hex(),
            endorsement,
        ),
    ]


def test_an_older_key_is_trusted_only_when_the_current_one_vouches_for_it(
    tmp_path: Path,
) -> None:
    old, new = keys.create(tmp_path / "old.key"), keys.create(tmp_path / "new.key")
    assert set(chain.trusted_keys(new.public, rotation(old, new, vouched=True))) == {
        old.fingerprint,
        new.fingerprint,
    }
    assert set(chain.trusted_keys(new.public, rotation(old, new, vouched=False))) == {
        new.fingerprint
    }


def test_a_rotation_pointing_at_a_missing_key_trusts_only_the_current_one(
    tmp_path: Path,
) -> None:
    old, new = keys.create(tmp_path / "old.key"), keys.create(tmp_path / "new.key")
    rows = rotation(old, new, vouched=True)[1:]  # the older key's own row is gone
    assert set(chain.trusted_keys(new.public, rows)) == {new.fingerprint}


def test_an_unreadable_row_is_named_after_the_part_of_the_log_it_breaks() -> None:
    assert chain.unreadable("audit_events").code == AUDIT_CHAIN_BROKEN
    assert chain.unreadable("audit_keys").code == AUDIT_CHECKPOINT_INVALID
    assert chain.unreadable("audit_checkpoints").details == {"seq": "0", "reason": "unreadable"}
