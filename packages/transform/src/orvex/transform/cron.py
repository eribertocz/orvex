"""Standard five-field cron expressions in an IANA time zone (P1-19).

Fields: minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-7, both 0
and 7 are Sunday). Each is `*`, a number, a range `a-b`, a step `*/n` or `a-b/n`, or a list of
those separated by commas. As in cron, when both the day of month and the day of week are
restricted (neither starts with `*`), a day matches when either does.

Times are local to the declared zone, through `zoneinfo`: a local time skipped by a daylight
saving change fires at the first local minute that exists after it; a local time that happens
twice fires once, at its first occurrence. No dependency: the common cron libraries are
maintained by one person (ADR-0002).
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12))
_WEEKDAY = ("day of week", 0, 7)
_SEARCH_DAYS = 366 * 8 + 2  # two leap-year cycles: every valid expression fires within them


class CronError(ValueError):
    """An expression that is not a valid five-field cron expression."""


@dataclass(frozen=True, slots=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday … 6 = Saturday
    either_day: bool  # both day fields restricted: a day matches when either does

    def matches_day(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        in_week = (day.isoweekday() % 7) in self.weekdays
        return (in_month or in_week) if self.either_day else (in_month and in_week)


def _number(text: str, name: str, low: int, high: int) -> int:
    if not text.isdigit() or not text.isascii():
        raise CronError(f"{name}: {text!r} is not a number.")
    value = int(text)
    if not low <= value <= high:
        raise CronError(f"{name}: {value} is outside {low}-{high}.")
    return value


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, slash, step_text = part.partition("/")
        step = _number(step_text, name, 1, high - low + 1) if slash else 1
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, _, last = body.partition("-")
            start, end = _number(first, name, low, high), _number(last, name, low, high)
            if start > end:
                raise CronError(f"{name}: the range {body} runs backwards.")
        else:
            start = _number(body, name, low, high)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse(text: str) -> Cron:
    """The expression's fields; `CronError` names the field when it is invalid or can never
    fire (for example `0 0 30 2 *`)."""
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields (minute hour day month weekday), got {len(parts)}.")
    minutes, hours, days, months = (
        _field(part, *spec) for part, spec in zip(parts[:4], _FIELDS, strict=True)
    )
    weekdays = frozenset(day % 7 for day in _field(parts[4], *_WEEKDAY))
    either = not parts[2].startswith("*") and not parts[4].startswith("*")
    cron = Cron(minutes, hours, days, months, weekdays, either)
    if not either and not any(
        day <= calendar.monthrange(2024, month)[1] for month in months for day in days
    ):
        raise CronError("the day of month never occurs in the listed months.")
    return cron


def _exists(local: dt.datetime, zone: ZoneInfo) -> bool:
    aware = local.replace(tzinfo=zone)
    return aware.astimezone(dt.UTC).astimezone(zone).replace(tzinfo=None) == local


def next_time(cron: Cron, after: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    """The first firing strictly after the aware instant `after`, as an aware UTC instant."""
    local = after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
    start = local + dt.timedelta(minutes=1)
    day = start.date()
    for _ in range(_SEARCH_DAYS):
        if cron.matches_day(day):
            for hour in sorted(cron.hours):
                for minute in sorted(cron.minutes):
                    candidate = dt.datetime.combine(day, dt.time(hour, minute))
                    if candidate < start:
                        continue
                    while not _exists(candidate, zone):  # skipped by a DST change
                        candidate += dt.timedelta(minutes=1)
                    instant = candidate.replace(tzinfo=zone, fold=0).astimezone(dt.UTC)
                    if instant > after:  # a repeated local time fires at its first occurrence
                        return instant
        day += dt.timedelta(days=1)
    raise CronError("the expression never fires.")  # unreachable for a parsed expression
