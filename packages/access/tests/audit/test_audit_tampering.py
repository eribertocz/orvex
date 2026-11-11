"""#437 AC2, R3, R4: `verify` finds every tampering an attacker with database access can do,
and names it. An attacker who also holds the signing key is caught by an anchor file shipped
outside the database before the tampering (Owner, 2026-11-10): the copy here lives in its own
directory, and nothing of it is read from, or written through, the store.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.access.audit import AuditLog, chain
from orvex.access.errors import AUDIT_CHAIN_BROKEN, AUDIT_CHECKPOINT_INVALID, AccessError
from orvex.access.storage import PostgresStore
from pg8000.exceptions import DatabaseError

from tests.support.access_directory import Raw, World

EVENTS = 10


@pytest.fixture
def log(world: World) -> AuditLog:
    """The world's log, writing a checkpoint every 4 events, with `EVENTS` more events."""
    made = AuditLog(world.store, world.audit.signing_key, world.audit.anchors, checkpoint_every=4)
    for _ in range(EVENTS):
        made.record_alone("auth.logout", world.admin, {})
    return made


@pytest.fixture
def raw(world: World) -> Iterator[Raw]:
    """The attacker's own connection. On SQLite it first drops the append-only triggers, as
    anyone who can write the file can; on PostgreSQL the superuser ignores the grants."""
    made = Raw(world.backend, world)
    if world.backend.kind == "sqlite":
        for (name,) in made.rows("SELECT name FROM sqlite_schema WHERE type = 'trigger'"):
            made.run(f"DROP TRIGGER {name}")
    yield made
    made.close()


def shipped(world: World, outside: Path) -> list[str]:
    """Ship the anchor file outside: a copy in a directory of its own, then read from there."""
    outside.mkdir()
    copy = outside / "anchors-shipped.log"
    shutil.copyfile(world.audit.anchors, copy)
    return copy.read_text("ascii").splitlines()


def rows(raw: Raw) -> list[tuple[int, str, str]]:
    return [
        (int(str(seq)), str(document), str(hashed))
        for seq, document, hashed in raw.rows(
            "SELECT seq, document, hash FROM audit_events ORDER BY seq"
        )
    ]


def rewrite(raw: Raw, change: int, key: AuditLog | None = None) -> None:
    """Change the actor of event `change`, then rebuild every later hash, the checkpoints' hashes
    and the head, so the chain is consistent again. With `key`, re-sign them too."""
    events = rows(raw)
    previous = chain.ZERO
    for seq, document, _ in events:
        if seq == change:
            changed = json.loads(document) | {"actor": "intruder"}
            document = json.dumps(changed, sort_keys=True, separators=(",", ":"))
        hashed = chain.link(previous, document.encode("utf-8"))
        raw.run(
            "UPDATE audit_events SET document = :document, hash = :hash WHERE seq = :seq",
            document=document,
            hash=hashed,
            seq=seq,
        )
        for at, signer in raw.rows(
            "SELECT at, key FROM audit_checkpoints WHERE seq = :seq", seq=seq
        ):
            signature = (
                key.signing_key.sign(chain.checkpoint_payload(seq, hashed, str(at), str(signer)))
                if key
                else b"\0" * 64
            )
            raw.run(
                "UPDATE audit_checkpoints SET hash = :hash, signature = :signature "
                "WHERE seq = :seq",
                hash=hashed,
                signature=signature.hex(),
                seq=seq,
            )
        previous = hashed
    head = key.signing_key.sign(chain.head_payload(events[-1][0], previous)) if key else b"\0" * 64
    raw.run(
        "UPDATE audit_head SET hash = :hash, signature = :signature WHERE id = 1",
        hash=previous,
        signature=head.hex(),
    )


def refused(log: AuditLog, anchors: list[str] | None = None) -> AccessError:
    with pytest.raises(AccessError) as caught:
        log.verify(anchors or [])
    return caught.value


def test_an_untouched_log_verifies(log: AuditLog, world: World) -> None:
    assert log.verify() == len(world.events())


def test_r3_a_changed_event_is_named(log: AuditLog, raw: Raw) -> None:
    seq, document, _ = rows(raw)[3]
    changed = json.loads(document) | {"actor": "intruder"}
    text = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    raw.run("UPDATE audit_events SET document = :d WHERE seq = :s", d=text, s=seq)
    problem = refused(log)
    assert problem.code == AUDIT_CHAIN_BROKEN
    assert problem.details == {"event": changed["id"], "seq": str(seq), "reason": "hash"}


def test_a_removed_event_is_found(log: AuditLog, raw: Raw) -> None:
    seq = rows(raw)[3][0]
    raw.run("DELETE FROM audit_events WHERE seq = :s", s=seq)
    problem = refused(log)
    assert problem.code == AUDIT_CHAIN_BROKEN
    assert problem.details["reason"] == "gap"
    assert problem.details["seq"] == str(seq + 1)


def test_reordered_events_are_found(log: AuditLog, raw: Raw) -> None:
    (first, one, h1), (second, two, h2) = rows(raw)[2:4]
    for seq, document, hashed in ((first, two, h2), (second, one, h1)):
        raw.run(
            "UPDATE audit_events SET id = :id, document = :d, hash = :h WHERE seq = :s",
            id=f"moved-{seq}",
            d=document,
            h=hashed,
            s=seq,
        )
    assert refused(log).code == AUDIT_CHAIN_BROKEN


def test_an_inserted_event_is_found(log: AuditLog, raw: Raw) -> None:
    """An event appended with a correct link: the head no longer matches, and the attacker
    cannot sign a new one."""
    seq, document, hashed = rows(raw)[-1]
    planted = json.loads(document) | {"id": "planted", "actor": "intruder"}
    text = json.dumps(planted, sort_keys=True, separators=(",", ":"))
    raw.run(
        "INSERT INTO audit_events (seq, id, at, kind, document, hash) "
        "VALUES (:s, 'planted', :at, :kind, :d, :h)",
        s=seq + 1,
        at=planted["at"],
        kind=planted["kind"],
        d=text,
        h=chain.link(hashed, text.encode("utf-8")),
    )
    assert refused(log).details["reason"] == "head"
    raw.run(
        "UPDATE audit_head SET seq = :s, hash = :h WHERE id = 1",
        s=seq + 1,
        h=chain.link(hashed, text.encode("utf-8")),
    )
    problem = refused(log)
    assert problem.code == AUDIT_CHECKPOINT_INVALID
    assert problem.details["reason"] == "head_signature"


def test_r4_a_forged_checkpoint_is_found(log: AuditLog, raw: Raw) -> None:
    raw.run("UPDATE audit_checkpoints SET signature = :s WHERE seq = 4", s="00" * 64)
    problem = refused(log)
    assert problem.code == AUDIT_CHECKPOINT_INVALID
    assert problem.details == {"seq": "4", "reason": "signature"}


def test_a_chain_rewritten_without_the_key_breaks_a_checkpoint_signature(
    log: AuditLog, raw: Raw
) -> None:
    rewrite(raw, change=2)
    problem = refused(log)
    assert problem.code == AUDIT_CHECKPOINT_INVALID
    assert problem.details["reason"] == "signature"


def test_a_chain_rewritten_with_the_key_is_caught_by_an_anchor_shipped_outside(
    log: AuditLog, raw: Raw, world: World, tmp_path: Path
) -> None:
    """The stated limit, and how the anchors close it: with the key, a rewrite verifies against
    the database alone, but not against anchors shipped before it."""
    anchors = shipped(world, tmp_path / "outside")
    assert anchors  # checkpoints 4 and 8 at least were anchored
    rewrite(raw, change=2, key=log)
    assert log.verify() == len(world.events())  # the documented limit
    problem = refused(log, anchors)
    assert problem.code == AUDIT_CHECKPOINT_INVALID
    assert problem.details["reason"] == "anchor"


def test_a_rollback_with_the_key_is_caught_by_an_anchor_shipped_outside(
    log: AuditLog, raw: Raw, world: World, tmp_path: Path
) -> None:
    """The newest events removed, and the head and checkpoints put back as they were signed
    then: only the anchors shipped before show it."""
    anchors = shipped(world, tmp_path / "outside")
    newest = max(json.loads(line)["seq"] for line in anchors)
    keep = newest - 1
    ((hashed,),) = raw.rows("SELECT hash FROM audit_events WHERE seq = :s", s=keep)
    raw.run("DELETE FROM audit_events WHERE seq > :s", s=keep)
    raw.run("DELETE FROM audit_checkpoints WHERE seq > :s", s=keep)
    signature = log.signing_key.sign(chain.head_payload(keep, str(hashed))).hex()
    raw.run(
        "UPDATE audit_head SET seq = :s, hash = :h, signature = :g WHERE id = 1",
        s=keep,
        h=hashed,
        g=signature,
    )
    assert log.verify() == keep  # the database alone cannot tell
    problem = refused(log, anchors)
    assert problem.details == {"seq": str(newest), "reason": "anchor"}


def test_events_removed_from_the_start_without_retention_are_found(
    log: AuditLog, raw: Raw, world: World, tmp_path: Path
) -> None:
    """A database writer deletes the oldest events up to a checkpoint: only retention may do
    that, and it says so in the chain. Caught with or without anchors."""
    anchors = shipped(world, tmp_path / "outside")
    raw.run("DELETE FROM audit_events WHERE seq <= 8")
    for given in ([], anchors):
        problem = refused(log, given)
        assert problem.code == AUDIT_CHAIN_BROKEN
        assert problem.details["reason"] == "start"


def test_a_rollback_without_the_key_is_caught_only_by_an_anchor_shipped_outside(
    log: AuditLog, raw: Raw, world: World, tmp_path: Path
) -> None:
    """The stated limit: the head as it was signed, put back after newer events, verifies
    against the database alone. A checkpoint anchored in between gives it away."""
    ((seq, hashed, at, key, signature),) = raw.rows(
        "SELECT seq, hash, at, key, signature FROM audit_head WHERE id = 1"
    )
    for _ in range(4):  # far enough to write and anchor a new checkpoint
        log.record_alone("auth.logout", world.admin, {})
    anchors = shipped(world, tmp_path / "outside")
    raw.run("DELETE FROM audit_events WHERE seq > :s", s=seq)
    raw.run("DELETE FROM audit_checkpoints WHERE seq > :s", s=seq)
    raw.run(
        "UPDATE audit_head SET seq = :s, hash = :h, at = :a, key = :k, signature = :g WHERE id = 1",
        s=seq,
        h=hashed,
        a=at,
        k=key,
        g=signature,
    )
    assert log.verify() == int(str(seq))  # the database alone cannot tell
    assert refused(log, anchors).details["reason"] == "anchor"


@pytest.mark.parametrize("backend", ["postgres"], indirect=True)
def test_an_owner_role_that_grants_itself_delete_is_still_caught(
    log: AuditLog, world: World
) -> None:
    """On PostgreSQL the store's role owns the tables and can grant itself DELETE again: the
    grants are defense in depth, and the chain is what catches the removal."""
    assert isinstance(world.store, PostgresStore)
    with world.store.transaction() as transaction:
        transaction.run("GRANT DELETE ON audit_events TO CURRENT_USER")
        transaction.run("DELETE FROM audit_events WHERE seq = 3")
    assert refused(log).code == AUDIT_CHAIN_BROKEN


def test_retention_refuses_a_log_whose_ages_were_changed(log: AuditLog, raw: Raw) -> None:
    """A database writer makes recent events look old: retention verifies first, finds the
    `at` columns no longer match the signed documents, and removes nothing."""
    before = raw.rows("SELECT count(*) FROM audit_events")
    raw.run("UPDATE audit_events SET at = '2001-01-01T00:00:00.000000Z' WHERE seq <= 8")
    with pytest.raises(AccessError) as caught:
        log.retain(30)
    assert caught.value.code == AUDIT_CHAIN_BROKEN
    assert caught.value.details["reason"] == "columns"
    assert raw.rows("SELECT count(*) FROM audit_events") == before


def test_a_log_emptied_without_retention_is_found(
    log: AuditLog, raw: Raw, world: World, tmp_path: Path
) -> None:
    """Every event deleted: retention always leaves its own event, so an empty log whose head
    has moved was emptied by someone else, even when the head sits on a checkpoint."""
    log.record_alone("auth.logout", world.admin, {})
    while int(str(raw.rows("SELECT seq FROM audit_head")[0][0])) % 4:
        log.record_alone("auth.logout", world.admin, {})
    anchors = shipped(world, tmp_path / "outside")
    raw.run("DELETE FROM audit_events")
    for given in ([], anchors):
        problem = refused(log, given)
        assert problem.code == AUDIT_CHAIN_BROKEN
        assert problem.details["reason"] == "start"


def test_a_forged_kind_column_is_a_broken_chain_never_a_crash(log: AuditLog, raw: Raw) -> None:
    """R3 holds for every planted row: a row whose kind column claims a retention it is not."""
    raw.run("DELETE FROM audit_events WHERE seq <= 4")
    raw.run("UPDATE audit_events SET kind = 'audit.retention' WHERE seq = 6")
    problem = refused(log)
    assert problem.code == AUDIT_CHAIN_BROKEN
    assert problem.details["seq"] == "6"
    with pytest.raises(AccessError) as caught:
        log.retain(30)  # retention verifies first, with the same answer
    assert caught.value.code == AUDIT_CHAIN_BROKEN


@pytest.mark.parametrize("planted", ["[]", '"x"', "not json", "{}"])
def test_a_planted_document_of_any_shape_names_its_event(
    log: AuditLog, raw: Raw, planted: str
) -> None:
    """R3 for every planted value: `verify` and `retain` name the event, never crash."""
    ((event,),) = raw.rows("SELECT id FROM audit_events WHERE seq = 3")
    raw.run("UPDATE audit_events SET document = :d WHERE seq = 3", d=planted)
    for attempt in (log.verify, lambda: log.retain(30)):
        with pytest.raises(AccessError) as caught:
            attempt()
        assert caught.value.code == AUDIT_CHAIN_BROKEN
        assert caught.value.details["event"] == event


@pytest.mark.parametrize("planted", ["zz", "", "0"])
def test_a_planted_signature_that_is_not_hex_is_an_invalid_checkpoint(
    log: AuditLog, planted: str
) -> None:
    """The stores' CHECK constraints refuse such a value; one who drops them still gets a
    named invalid checkpoint from `verify`, never a crash (checked on the rows directly)."""
    checkpoint = log.checkpoints()[0]
    forged = chain.CheckpointRow(
        checkpoint.seq, checkpoint.hash, checkpoint.at, checkpoint.key, planted
    )
    head = chain.HeadRow(checkpoint.seq, checkpoint.hash, checkpoint.key, planted)
    trusted = {checkpoint.key: log.signing_key.public}
    with pytest.raises(AccessError) as caught:
        chain.verify([], [forged], head, trusted)
    assert caught.value.code == AUDIT_CHECKPOINT_INVALID
    assert caught.value.details == {"seq": str(checkpoint.seq), "reason": "signature"}


def test_a_planted_key_that_is_not_ascii_is_an_invalid_checkpoint(log: AuditLog) -> None:
    """Both stores' CHECK constraints refuse it; past them, `verify` still names it."""
    checkpoint = log.checkpoints()[0]
    forged = chain.CheckpointRow(
        checkpoint.seq, checkpoint.hash, checkpoint.at, "sha256:" + "\u00e9" * 64, "0" * 128
    )
    head = chain.HeadRow(checkpoint.seq, checkpoint.hash, None, None)
    with pytest.raises(AccessError) as caught:
        chain.verify([], [forged], head, {})
    assert caught.value.details == {"seq": str(checkpoint.seq), "reason": "signature"}


def test_the_stores_refuse_a_key_that_is_not_a_fingerprint(raw: Raw) -> None:
    with pytest.raises((sqlite3.IntegrityError, DatabaseError)):
        raw.run(
            "INSERT INTO audit_checkpoints VALUES (2, :h, '2026-11-10T00:00:00.000000Z', :k, :s)",
            h="0" * 64,
            k="sha256:" + "\u00e9" * 64,
            s="0" * 128,
        )
