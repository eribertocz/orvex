"""The bindings and their codec (#486 G5): values cross serialized exactly, live objects never."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

import pytest
from orvex.contracts.binding import Bound
from orvex.contracts.codec import Codec
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit_run import run_kit, verify_kit
from pydantic import BaseModel

from .sample_contract import (
    ENVELOPE,
    LEDGER,
    LEDGER_KIT,
    READING,
    LedgerError,
    MemoryLedger,
    Reading,
)


class Color(Enum):
    RED = "red"


class Shape(BaseModel):
    side: Decimal


@dataclass(frozen=True, slots=True)
class Undeclared:
    value: int


@dataclass(frozen=True, slots=True)
class ReadingChild(Reading):
    pass


CODEC = Codec(frozenset({Reading, Color, Shape, CallEnvelope}))

VALUES: list[object] = [
    None,
    True,
    False,
    0,
    -(2**127),
    2**128 - 1,
    1.5,
    float("inf"),
    -0.0,
    "",
    "ñandú ✓",
    b"\x00\xff",
    Decimal("12.50"),
    Decimal("-0.000"),
    Decimal("123456789012345678901234567890.123456789"),
    datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/La_Paz")),
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ZoneInfo("America/New_York")),
    datetime(2026, 3, 8, 1, 30, 0, 123456),
    datetime(2026, 3, 8, tzinfo=UTC),
    datetime(2026, 3, 8, tzinfo=timezone(timedelta(hours=-4))),
    date(2026, 2, 28),
    time(23, 59, 59, 999999),
    time(1, 2, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    timedelta(days=-1, seconds=5, microseconds=7),
    (1, "a", (Decimal("1.0"),)),
    [1, [2, None]],
    {"a": {"b": [Decimal("0.10")]}, "": None},
    Color.RED,
    Shape(side=Decimal("2.50")),
    READING,
    ENVELOPE,
    (),
    [],
    {},
]


def same(left: object, right: object) -> bool:
    """Equal, with the same type all the way down (a tuple never comes back a list)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, (tuple, list)):
        assert isinstance(right, (tuple, list))
        return len(left) == len(right) and all(map(same, left, right, strict=True))
    if isinstance(left, dict):
        assert isinstance(right, dict)
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, Decimal):
        return str(left) == str(right)
    if isinstance(left, (datetime, time)):
        assert isinstance(right, (datetime, time))
        return (
            left == right
            and str(left.tzinfo) == str(right.tzinfo)
            and left.fold == right.fold
            and left.isoformat() == right.isoformat()
        )
    return left == right or (left != left and right != right)


class TestCodec:
    @pytest.mark.parametrize("value", VALUES, ids=repr)
    def test_every_value_comes_back_exactly(self, value: object) -> None:
        back = CODEC.decode(CODEC.encode(value))
        assert same(value, back), (value, back)

    def test_a_zone_comes_back_as_the_same_zone(self) -> None:
        back = CODEC.decode(CODEC.encode(READING.at))
        assert isinstance(back, datetime)
        assert back.tzinfo == ZoneInfo("America/La_Paz")

    @pytest.mark.parametrize(
        "value",
        [
            threading.Lock(),
            iter([1]),
            lambda: 1,
            object(),
            Undeclared(1),
            ReadingChild("x", Decimal(1), READING.at),
            {1: "a"},
            {"a": threading.Lock()},
            {1, 2},
            bytearray(b"x"),
            time(1, tzinfo=ZoneInfo("Europe/Madrid")),
        ],
        ids=lambda value: type(value).__name__,
    )
    def test_a_live_or_undeclared_value_is_refused(self, value: object) -> None:
        with pytest.raises(InterfaceContractError) as raised:
            CODEC.encode(value)
        assert raised.value.code == "INTERFACE_VALUE_NOT_SERIALIZABLE"

    @pytest.mark.parametrize(
        "data",
        [b"not json", b'{"$unknown": 1}', b'{"$dataclass": "nowhere.Thing", "fields": {}}', b"{}"],
    )
    def test_bytes_that_are_not_an_encoded_value_are_refused(self, data: bytes) -> None:
        with pytest.raises(InterfaceContractError) as raised:
            CODEC.decode(data)
        assert raised.value.code == "INTERFACE_VALUE_NOT_SERIALIZABLE"


class LiveNames(tuple[str, ...]):
    """A live view of the names: equal to the tuple in process, but it holds a lock."""

    lock = threading.Lock()


class LiveLedger(MemoryLedger):
    """A planted violation: `names` hands out a live object instead of a value."""

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]:
        return LiveNames(super().names(envelope))


class WrongCode(MemoryLedger):
    def readings(self, envelope: CallEnvelope, name: str) -> tuple[Reading, ...]:
        raise LedgerError("LEDGER_GONE", "not declared for readings", {})


class Crashing(MemoryLedger):
    def readings(self, envelope: CallEnvelope, name: str) -> tuple[Reading, ...]:
        raise KeyError(name)


class Seen(MemoryLedger):
    """Keeps every envelope it is called with."""

    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[CallEnvelope] = []

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]:
        self.envelopes.append(envelope)
        return super().names(envelope)


class TestBound:
    @pytest.mark.parametrize("serialized", [False, True])
    def test_only_the_contract_s_operations_are_reachable(self, serialized: bool) -> None:
        bound = Bound(LEDGER, MemoryLedger(), serialized=serialized)
        assert bound.serialized is serialized
        with pytest.raises(AttributeError):
            bound.__getattr__("_readings")
        with pytest.raises(AttributeError):
            bound.__getattr__("drop")

    @pytest.mark.parametrize("serialized", [False, True])
    def test_an_implementation_without_the_operation_is_refused(self, serialized: bool) -> None:
        class Partial:
            def readings(self, envelope: CallEnvelope, name: str) -> tuple[Reading, ...]:
                return ()

        bound = Bound(LEDGER, Partial(), serialized=serialized)
        with pytest.raises(InterfaceContractError) as raised:
            bound.names(ENVELOPE)
        assert raised.value.code == "INTERFACE_OPERATION_MISSING"

    @pytest.mark.parametrize("serialized", [False, True])
    def test_the_implementation_receives_the_envelope(self, serialized: bool) -> None:
        ledger = Seen()
        Bound(LEDGER, ledger, serialized=serialized).names(ENVELOPE)
        assert ledger.envelopes == [ENVELOPE]
        assert (ledger.envelopes[0] is ENVELOPE) is not serialized

    def test_serialized_calls_cross_as_copies(self) -> None:
        ledger = MemoryLedger()
        Bound(LEDGER, ledger, serialized=True).record(ENVELOPE, READING)
        kept = ledger.readings(ENVELOPE, READING.name)
        assert kept == (READING,)
        assert kept[0] is not READING
        again = Bound(LEDGER, ledger, serialized=True).readings(ENVELOPE, READING.name)
        assert again[0] is not kept[0]
        in_process = MemoryLedger()
        Bound(LEDGER, in_process, serialized=False).record(ENVELOPE, READING)
        assert in_process.readings(ENVELOPE, READING.name)[0] is READING

    def test_a_declared_error_crosses_with_its_code_message_and_details(self) -> None:
        bound = Bound(LEDGER, MemoryLedger(), serialized=True)
        with pytest.raises(LedgerError) as raised:
            bound.record(ENVELOPE, Reading("", Decimal(1), READING.at))
        assert raised.value.code == "LEDGER_NAME_EMPTY"
        assert raised.value.message == "a reading needs a name"
        assert raised.value.details == {"field": "name"}
        assert raised.value.__cause__ is None

    @pytest.mark.parametrize("serialized", [False, True])
    @pytest.mark.parametrize("implementation", [WrongCode, Crashing])
    def test_an_undeclared_error_is_refused_in_both_bindings(
        self, serialized: bool, implementation: type[MemoryLedger]
    ) -> None:
        with pytest.raises(InterfaceContractError) as raised:
            Bound(LEDGER, implementation(), serialized=serialized).readings(ENVELOPE, "a")
        assert raised.value.code == "INTERFACE_ERROR_UNDECLARED"
        assert (raised.value.__cause__ is None) is serialized

    def test_a_live_result_is_refused_when_serialized(self) -> None:
        ledger = LiveLedger()
        assert Bound(LEDGER, ledger, serialized=False).names(ENVELOPE) == ()
        with pytest.raises(InterfaceContractError) as raised:
            Bound(LEDGER, ledger, serialized=True).names(ENVELOPE)
        assert raised.value.code == "INTERFACE_VALUE_NOT_SERIALIZABLE"


class TestPlantedLiveObject:
    """G5: a planted operation that returns a live object fails the serializing run."""

    def test_the_kit_fails_the_serialized_run_and_the_runs_differ(self) -> None:
        serialized = run_kit(LEDGER_KIT, LiveLedger, serialized=True)
        failed = [result.id for result in serialized.results if not result.passed]
        assert failed == ["names.sorted"]
        assert run_kit(LEDGER_KIT, LiveLedger, serialized=False).results[3].passed
        with pytest.raises(InterfaceContractError) as raised:
            verify_kit(LEDGER_KIT, LiveLedger)
        assert raised.value.code == "KIT_FAILED"
        assert "KIT_BINDINGS_DIFFER" in raised.value.message
        assert "names.sorted (serialized, consumer 1.1, provider 1.1)" in raised.value.message
