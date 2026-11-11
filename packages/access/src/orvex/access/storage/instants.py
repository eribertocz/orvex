"""The stored form of an instant (domain): UTC, microseconds, `Z`, so instants compare as text."""

from __future__ import annotations

from datetime import UTC, datetime


def instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parsed(stored: str) -> datetime:
    return datetime.strptime(stored, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
