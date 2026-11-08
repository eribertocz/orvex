"""Instants in a zone on ClickHouse (C6, P2-19; #225): an instant's calendar bucket in its zone,
and the check of the engine's zone data against the pinned `tzdata`.

ClickHouse's `toStartOfHour`, `toStartOfInterval` and their kind answer a 32-bit `DateTime`,
which cannot hold an instant before 1970 or after 2106. So an instant is read as its microseconds
since the epoch (`DateTime64(6)`), and every bucket is computed from them and the zone's offset at
that instant, as the reference does:

- a day or longer is the local date the instant falls on (`Date32`), then cut as a date;
- an hour or a minute stays an instant: the instant minus its local time within the unit, so an
  hour that repeats when clocks go back stays two rows, each with its offset.

The zone is a bound value; every zone the statement uses is checked first (`CHECK_SQL`).
"""

from __future__ import annotations

_CLOCK = {"hour": 3_600_000_000, "minute": 60_000_000}  # the unit in microseconds

# The engine counts the points it converts differently from the pinned data: 0 when it agrees.
# p0: UTC epoch seconds, p1: the expected local wall-clock epoch seconds, p2: the zone
# (`zonedata.parameters`).
CHECK_SQL = (
    'SELECT toInt64(count()) AS "#different" FROM (SELECT arrayJoin(arrayZip('
    "arrayMap(x -> toInt64(x), splitByChar(',', {p0:String})), "
    'arrayMap(x -> toInt64(x), splitByChar(\',\', {p1:String})))) AS "#v") AS "#t"'
    ' WHERE tupleElement("#v", 1) + toInt64(timeZoneOffset('
    'toDateTime64(tupleElement("#v", 1), 0, {p2:String}))) <> tupleElement("#v", 2)'
)


def local_date(value: str, zone: str) -> str:
    """The local date an instant falls on in the bound zone `zone`."""
    return f"toDate32(toTimeZone(toDateTime64({value}, 6, 'UTC'), {zone}))"


def clock_start(unit: str, value: str, zone: str) -> str:
    """The instant the local hour or minute holding `value` starts at, in microseconds."""
    instant = f"toDateTime64({value}, 6, 'UTC')"
    micros = f"toUnixTimestamp64Micro({instant})"
    offset = f"toInt64(timeZoneOffset(toTimeZone({instant}, {zone}))) * 1000000"
    within = f"positiveModulo({micros} + {offset}, {_CLOCK[unit]})"
    return f"fromUnixTimestamp64Micro({micros} - {within}, 'UTC')"


def clock_unit(unit: str) -> bool:
    return unit in _CLOCK
