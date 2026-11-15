"""#486 G5: values the codec must carry exactly that the store contract showed it refused: a model
with field aliases, and an instant whose fixed offset is pydantic's own zone class."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from orvex.contracts.codec import Codec
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.store.v1 import LakeTable, Snapshot, TableColumn


def test_a_model_with_an_aliased_field_crosses_exactly() -> None:
    column = TableColumn(name="a", type="DECIMAL(18,2)", nullable=False)
    table = LakeTable(schema="main", table="t", columns=[column])
    codec = Codec(frozenset({LakeTable}))
    assert codec.decode(codec.encode(table)) == table


def test_an_instant_parsed_by_pydantic_keeps_its_offset() -> None:
    snapshot = Snapshot.model_validate(
        {"kind": "snapshot", "id": 1, "committed_at": "2026-03-29T01:30:00+02:00"}
    )
    assert type(snapshot.committed_at.tzinfo) is not dt.timezone  # pydantic's own class
    codec = Codec(frozenset({Snapshot}))
    crossed = codec.decode(codec.encode(snapshot.committed_at))
    assert isinstance(crossed, dt.datetime)
    assert crossed == snapshot.committed_at
    assert crossed.utcoffset() == dt.timedelta(hours=2)


class _Shifting(dt.tzinfo):
    """A zone whose offset depends on the instant: not a fixed offset, and not an IANA key."""

    def utcoffset(self, moment: dt.datetime | None) -> dt.timedelta:
        return dt.timedelta(hours=1 if moment is None or moment.month < 6 else 2)

    def dst(self, moment: dt.datetime | None) -> dt.timedelta:
        return dt.timedelta(0)


def test_a_zone_that_is_neither_fixed_nor_named_is_refused() -> None:
    codec = Codec(frozenset())
    with pytest.raises(InterfaceContractError) as caught:
        codec.encode(dt.datetime(2026, 7, 1, tzinfo=_Shifting()))
    assert caught.value.code == "INTERFACE_VALUE_NOT_SERIALIZABLE"
    named = dt.datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Lima"))
    assert codec.decode(codec.encode(named)) == named
