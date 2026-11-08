"""Receipts of reads on an existing DuckLake whose snapshots are no longer kept (P2-30 PR 3,
#399; ADR-0027 §5).

An external snapshot is identified by the project, the connection, its id and its commit time,
never by the id alone: a restored or replaced catalog can reuse ids for other data. When the
lake is treated as new (a restored catalog, an id lower than the last one verified), or its
owner expires a snapshot, a receipt that read one of the snapshots it no longer keeps cannot be
replayed any more. `expired` gives that record's successor: the same document, each such read
marked not `retained`, and its reproducibility `explainable` (`snapshot_expired`), sealed again
as `verify_replay` does. Nothing else of the record changes, and a record whose external
snapshots are all still kept is returned as it is.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orvex.semantic.receipt.building import Key, ReceiptRecord

EXPIRED = "snapshot_expired"
# The identity of one external snapshot: project, connection, id and commit time (an instant).
Identity = tuple[str, str, int, str]


def identity(read: dict[str, Any]) -> Identity:
    """The identity of the external snapshot one of a record's reads names."""
    return (
        str(read["project"]),
        str(read["connection"]),
        int(read["snapshot_id"]),
        str(read["committed_at"]),
    )


def expired(
    record: ReceiptRecord, kept: Callable[[Identity], bool], seal_key: Key
) -> ReceiptRecord:
    """`record`, or its `explainable` successor when `kept` says one of the external snapshots
    it read is no longer kept."""
    document = record.document()
    changed = False
    for read in document["data"]:
        if read.get("route") != "existing_ducklake" or not read.get("retained"):
            continue
        if not kept(identity(read)):
            read["retained"] = False
            changed = True
    if not changed:
        return record
    reasons = list(document["reproducibility"].get("reasons", ()))
    document["reproducibility"] = {
        "class": "explainable",
        "reasons": [EXPIRED, *(r for r in reasons if r != EXPIRED)],
        "replay": "not_checked",
    }
    return ReceiptRecord(document, seal_key)
