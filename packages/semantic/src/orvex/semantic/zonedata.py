"""The engine's time-zone data, checked against the pinned `tzdata` (C6, P2-19; #337).

DuckDB converts instants with its built-in ICU library, whose zone data version it does not
report. The model fingerprint records the pinned `tzdata` version (`compiler/zones.py`), so a
zone is used only once the engine is shown to agree with it: on both sides of every change of
the zone's UTC offset from 1970 to 2100, at both ends of that range, and once a week between,
so a change only the engine has is seen too.

**Once per zone and connection, with a safe cache** (Owner decision, 2026-11-04):
- the cache key is the engine's reader (its connection), the zone and the pinned `tzdata`
  version, so a new connection or a new `tzdata` checks again;
- only a passed check is kept; a failure is never cached, and is checked again next time;
- concurrent first questions on one connection run the check once: the others wait for it.

A reader that cannot be weakly referenced is never cached: it is checked every time.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

from orvex.semantic.compiler.zones import TZDATA_VERSION

START = datetime(1970, 1, 1, tzinfo=UTC)
END = datetime(2100, 1, 1, tzinfo=UTC)
_SECOND = timedelta(seconds=1)

# The engine counts the points it converts differently from the pinned data: 0 when it agrees.
# $p0: UTC epoch seconds, $p1: the expected local wall-clock epoch seconds, $p2: the zone.
CHECK_SQL = (
    'SELECT count(*) AS "#different" FROM (SELECT'
    " CAST(unnest(string_split($p0, ',')) AS BIGINT) AS t,"
    " CAST(unnest(string_split($p1, ',')) AS BIGINT) AS l) AS v"
    " WHERE datediff('second', TIMESTAMP '1970-01-01 00:00:00',"
    " timezone($p2, to_timestamp(t))) <> l"
)


def pinned(zone: str) -> ZoneInfo:
    """The zone from the pinned `tzdata` package, never the host's database."""
    path = resources.files("tzdata").joinpath("zoneinfo", *zone.split("/"))
    with path.open("rb") as handle:
        return ZoneInfo.from_file(handle, key=zone)


@cache
def points(zone: str) -> tuple[tuple[int, int], ...]:
    """(UTC epoch second, local wall-clock epoch second) on both sides of every offset change
    from 1970 to 2100, at both ends, and every week between, by the pinned `tzdata`."""
    tz = pinned(zone)

    def offset(at: datetime) -> timedelta:
        found = at.astimezone(tz).utcoffset()
        assert found is not None  # a zone always has an offset
        return found

    # Every week too: a change the engine has and the pinned data lacks is seen within a week.
    instants = [START + timedelta(days=7 * n) for n in range((END - START).days // 7 + 1)]
    instants += [END - _SECOND]
    day, before = START, offset(START)
    while day < END:
        after_day = day + timedelta(days=1)
        changed = offset(after_day)
        if changed != before:  # the change is in this day: find its second
            low, high = day, after_day
            while high - low > _SECOND:
                middle = low + timedelta(seconds=(high - low) // _SECOND // 2)
                if offset(middle) == before:
                    low = middle
                else:
                    high = middle
            instants += [low, high]
            before = changed
        day = after_day
    return tuple(
        (int(at.timestamp()), int(at.timestamp() + offset(at).total_seconds()))
        for at in sorted(set(instants))
    )


def parameters(zone: str) -> tuple[dict[str, str | None], ...]:
    found = points(zone)
    return (
        {"name": "p0", "type": "VARCHAR", "value": ",".join(str(t) for t, _ in found)},
        {"name": "p1", "type": "VARCHAR", "value": ",".join(str(local) for _, local in found)},
        {"name": "p2", "type": "VARCHAR", "value": zone},
    )


class ZoneDataMismatch(Exception):
    """The engine converts a zone differently from the pinned `tzdata`."""


_guard = threading.Lock()
_passed: weakref.WeakKeyDictionary[object, set[tuple[str, str]]] = weakref.WeakKeyDictionary()
_locks: weakref.WeakKeyDictionary[object, dict[str, threading.Lock]] = weakref.WeakKeyDictionary()


def verify(zones: tuple[str, ...], owner: object, count: Callable[[str], int]) -> None:
    """Each zone checked once per `owner` (the engine's connection): `count(zone)` runs the
    check on the engine and gives how many points differ. Raises `ZoneDataMismatch`."""
    for zone in zones:
        key = (zone, TZDATA_VERSION)
        try:
            with _guard:
                passed = _passed.setdefault(owner, set())
                lock = _locks.setdefault(owner, {}).setdefault(zone, threading.Lock())
        except TypeError:  # cannot be weakly referenced: never cached
            if count(zone):
                raise ZoneDataMismatch(zone) from None
            continue
        with lock:  # one check per zone and connection; the others wait for its result
            with _guard:
                done = key in passed
            if done:
                continue
            if count(zone):
                raise ZoneDataMismatch(zone)
            with _guard:
                passed.add(key)
