"""A small interface contract at version 1.1, its implementations at 1.1 and 1.0, and its kit,
for the tests of the format itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.version import Version
from pydantic import AwareDatetime


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
    at: AwareDatetime


class Ledger(Protocol):
    def record(self, envelope: CallEnvelope, reading: Reading) -> int: ...

    def readings(self, envelope: CallEnvelope, name: str) -> tuple[Reading, ...]: ...

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]: ...

    def count(self, envelope: CallEnvelope) -> int: ...


LEDGER = InterfaceContract(
    name="ledger",
    version=Version(1, 1),
    protocol=Ledger,
    error_type=LedgerError,
    capabilities=("read", "write"),
    errors=("LEDGER_NAME_EMPTY",),
    operations=(
        Operation("record", "write", ("LEDGER_NAME_EMPTY",), changes_state=True),
        Operation("readings", "read"),
        Operation("names", "read"),
        Operation("count", "read", since=Version(1, 1)),
    ),
)


class MemoryLedgerV10:
    """A correct implementation of the ledger at version 1.0, the released one: no `count`."""

    def __init__(self) -> None:
        self._readings: list[Reading] = []
        self._done: dict[str, int] = {}

    def record(self, envelope: CallEnvelope, reading: Reading) -> int:
        if not reading.name:
            raise LedgerError("LEDGER_NAME_EMPTY", "a reading needs a name", {"field": "name"})
        key = envelope.idempotency_key or ""
        if key not in self._done:
            self._readings.append(reading)
            self._done[key] = len(self._readings)
        return self._done[key]

    def readings(self, envelope: CallEnvelope, name: str) -> tuple[Reading, ...]:
        return tuple(reading for reading in self._readings if reading.name == name)

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]:
        return tuple(sorted({reading.name for reading in self._readings}))


class MemoryLedger(MemoryLedgerV10):
    """A correct implementation of the ledger at version 1.1."""

    def count(self, envelope: CallEnvelope) -> int:
        return len(self._readings)


READING = Reading(
    "meter-7", Decimal("12.50"), datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/La_Paz"))
)


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def round_trip(ledger: KitClient) -> None:
    ledger.record(READING)
    found = ledger.readings(READING.name)
    _expect(found == (READING,), f"read back {found!r}")
    _expect(str(found[0].amount) == "12.50", "the scale changed")
    _expect(found[0].at.tzinfo == READING.at.tzinfo, "the zone changed")


def missing(ledger: KitClient) -> None:
    _expect(ledger.readings("absent") == (), "an unknown name has readings")


def empty_name_refused(ledger: KitClient) -> None:
    try:
        ledger.record(Reading("", Decimal(1), READING.at))
    except LedgerError as error:
        _expect(error.code == "LEDGER_NAME_EMPTY", f"code {error.code}")
        _expect(error.details == {"field": "name"}, f"details {error.details}")
        return
    raise AssertionError("an empty name was accepted")


def names_sorted(ledger: KitClient) -> None:
    for name in ("b", "a", "b"):
        ledger.record(Reading(name, Decimal(0), READING.at))
    _expect(ledger.names() == ("a", "b"), "names are not sorted and distinct")


def counted(ledger: KitClient) -> None:
    for name in ("a", "a"):
        ledger.record(Reading(name, Decimal(0), READING.at))
    _expect(ledger.count() == 2, "the count is not the number of readings")


def record_once(ledger: KitClient, envelope: CallEnvelope) -> object:
    return ledger.send("record", envelope, READING)


def observe_readings(ledger: KitClient) -> object:
    return ledger.readings(READING.name)


LEDGER_KIT = Kit(
    contract=LEDGER,
    recorded=6,
    cases=(
        KitCase(
            "record.round-trip-exact",
            "record",
            "a reading read back keeps its name, amount scale and time zone",
            round_trip,
        ),
        KitCase("readings.missing-is-empty", "readings", "an unknown name has no reading", missing),
        KitCase(
            "record.empty-name-refused",
            "record",
            "an empty name is refused with LEDGER_NAME_EMPTY",
            empty_name_refused,
        ),
        KitCase("names.sorted", "names", "names come back sorted and distinct", names_sorted),
        KitCase(
            "record.idempotent",
            "record",
            "a reading recorded twice with one idempotency key is kept once, with one number",
            Idempotent(record_once, observe_readings),
        ),
        KitCase(
            "count.readings",
            "count",
            "the count is the number of readings recorded",
            counted,
            since=Version(1, 1),
        ),
    ),
)

# A complete envelope for direct calls in the tests (a fresh key where an effect is counted).
ENVELOPE = CallEnvelope(
    contract_name="ledger",
    contract_version="1.1",
    correlation_id="correlation-1",
    actor="actor-1",
    project_id="project-1",
    revision="revision-1",
    idempotency_key="key-1",
)
