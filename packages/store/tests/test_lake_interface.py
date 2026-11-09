"""#246 item 5: the names importable from the store's façade (`orvex.store.lake`), recorded in
`fixtures/lake_interface.json`.

- The public group: the names `lake.py` exports (defined there, or imported as `NAME as NAME`),
  each with its shape (a function's signature, a class's public methods and fields, a
  constant's value). Any change to it fails, an added or removed name included.
- The internal group: the underscore names bound in the façade (compatibility only, never a
  promise). It may only shrink: a new one fails, and one that is gone fails until the record
  drops it.
"""

from __future__ import annotations

import json
from pathlib import Path

import orvex.store.lake as facade

from tests.support.facade_interface import interface, internal

RECORD = Path(__file__).with_name("fixtures") / "lake_interface.json"


def test_the_public_group_is_unchanged() -> None:
    recorded = json.loads(RECORD.read_text(encoding="utf-8"))["public"]
    assert interface(facade)["public"] == recorded


def test_the_internal_group_only_shrinks() -> None:
    recorded = set(json.loads(RECORD.read_text(encoding="utf-8"))["internal"])
    found = set(internal(facade))
    assert not found - recorded, f"new internal names in the façade: {sorted(found - recorded)}"
    assert not recorded - found, f"gone from the façade, drop from the record: {recorded - found}"
