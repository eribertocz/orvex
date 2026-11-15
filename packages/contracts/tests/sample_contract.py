"""A small interface contract, an implementation and a kit for the tests of the format itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from orvex.contracts.binding import Bound
from orvex.contracts.interface import InterfaceContract, Operation, Version
from orvex.contracts.kit import Kit, KitCase


class LedgerError(Exception):
    """The ledger's one error type: a stable code, a message and details."""

    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class Reading:
    name: str
    amount: Decimal
    at: datetime


class Ledger(Protocol):
    def put(self, reading: Reading) -> None: ...

    def get(self, name: str) -> Reading | None: ...

    def names(self) -> tuple[str, ...]: ...


LEDGER = InterfaceContract(
    name="ledger",
    version=Version(1, 0),
    protocol=Ledger,
    error_type=LedgerError,
    capabilities=("read", "write"),
    errors=("LEDGER_NAME_EMPTY",),
    operations=(
        Operation("put", "write", ("LEDGER_NAME_EMPTY",)),
        Operation("get", "read"),
        Operation("names", "read"),
    ),
)


class MemoryLedger:
    """A correct implementation of the ledger."""

    def __init__(self) -> None:
        self._readings: dict[str, Reading] = {}

    def put(self, reading: Reading) -> None:
        if not reading.name:
            raise LedgerError("LEDGER_NAME_EMPTY", "a reading needs a name", {"field": "name"})
        self._readings[reading.name] = reading

    def get(self, name: str) -> Reading | None:
        return self._readings.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._readings))


READING = Reading(
    "meter-7", Decimal("12.50"), datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/La_Paz"))
)


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def round_trip(ledger: Bound) -> None:
    ledger.put(READING)
    found = ledger.get(READING.name)
    _expect(found == READING, f"read back {found!r}")
    _expect(isinstance(found, Reading) and str(found.amount) == "12.50", "the scale changed")
    _expect(isinstance(found, Reading) and found.at.tzinfo == READING.at.tzinfo, "the zone changed")


def missing(ledger: Bound) -> None:
    _expect(ledger.get("absent") is None, "a missing name is not None")


def empty_name_refused(ledger: Bound) -> None:
    try:
        ledger.put(Reading("", Decimal(1), READING.at))
    except LedgerError as error:
        _expect(error.code == "LEDGER_NAME_EMPTY", f"code {error.code}")
        _expect(error.details == {"field": "name"}, f"details {error.details}")
        return
    raise AssertionError("an empty name was accepted")


def names_sorted(ledger: Bound) -> None:
    for name in ("b", "a"):
        ledger.put(Reading(name, Decimal(0), READING.at))
    _expect(ledger.names() == ("a", "b"), "names are not sorted")


LEDGER_KIT = Kit(
    contract=LEDGER,
    recorded=4,
    cases=(
        KitCase(
            "put.round-trip-exact",
            "put",
            "a reading read back keeps its name, amount scale and time zone",
            round_trip,
        ),
        KitCase("get.missing-is-none", "get", "an unknown name reads as None", missing),
        KitCase(
            "put.empty-name-refused",
            "put",
            "an empty name is refused with LEDGER_NAME_EMPTY",
            empty_name_refused,
        ),
        KitCase("names.sorted", "names", "names come back sorted", names_sorted),
    ),
)
