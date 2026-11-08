"""The verified snapshot of one existing DuckLake, checked on its schedule (P2-30 PR 3, #399;
ADR-0027 §5, §7).

Questions on an existing lake read only its latest **verified** snapshot, identified by the
project, the connection, its id and its commit time. `SnapshotSchedule.check` runs on the
connection's `schedule` (at least 5 minutes, default 1 hour; `due`):

1. **A restored catalog.** The served snapshot must still be listed with its commit time, and
   the latest id must not be lower than the last one verified. Otherwise the lake is treated as
   new: nothing is served until a snapshot passes, the state is red with the reason, and every
   snapshot verified before is no longer `kept`, so receipts naming one become `explainable`
   (`orvex.semantic.receipt.external`). A read that finds its pinned snapshot changed reports it
   the same way (`restored`).
2. **The served snapshot, again**, against the instance's protected set as it stands now
   (`verify` with a freshly built attachment). If it no longer passes, nothing is served.
3. **A new snapshot** is served only once its files pass `verify` and its evidence passes
   (`evidence`, which names what is violated, or None). If either fails, it is held back: the
   last verified snapshot stays served, and the state is red with the reason, until a later
   snapshot passes.

The state is red with a reason, amber when the served snapshot is older than the connection's
`freshness`, and green otherwise. Every decision is the schedule's alone; the store's checks
and the evidence are its callers'.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from orvex.contracts.store.v1 import ExistingLakeAttachment, Snapshot
from orvex.store.errors import StoreError

MINIMUM = dt.timedelta(minutes=5)
DEFAULT_SCHEDULE = "1h"
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
Color = Literal["green", "amber", "red"]
# An external snapshot's identity, as a receipt names it: project, connection, id, commit time.
Identity = tuple[str, str, int, str]


def period(text: str | None, *, minimum: dt.timedelta = dt.timedelta(0)) -> dt.timedelta:
    """`<n>m`, `<n>h` or `<n>d` as a duration, refused below `minimum`."""
    written = text or DEFAULT_SCHEDULE
    unit = _UNITS.get(written[-1:])
    if unit is None or not written[:-1].isdigit():
        raise ValueError(f"not a duration: {written!r}")
    found = dt.timedelta(**{unit: int(written[:-1])})
    if found < minimum:
        raise ValueError(f"{written!r} is shorter than {minimum}")
    return found


class Reads(Protocol):
    """What the schedule asks of the lake (`orvex.store.existing_lake_reads.LakeReads`)."""

    def latest(self) -> Snapshot: ...

    def committed_at(self, snapshot: int) -> dt.datetime | None: ...

    def verify(self, snapshot: Snapshot, current: ExistingLakeAttachment) -> Snapshot: ...


@dataclass(frozen=True, slots=True)
class State:
    color: Color
    reason: str | None  # why it is red or amber; None when green
    served: Snapshot | None


class SnapshotSchedule:
    """The served snapshot of the lake `connection` of `project`, and its state."""

    def __init__(
        self,
        project: str,
        connection: str,
        reads: Reads,
        *,
        attachment: Callable[[], ExistingLakeAttachment],
        evidence: Callable[[Snapshot], str | None],
        schedule: str | None,
        freshness: str | None,
    ) -> None:
        self.project = project
        self.connection = connection
        self._reads = reads
        self._attachment = attachment
        self._evidence = evidence
        self.every = period(schedule, minimum=MINIMUM)
        self._freshness = period(freshness) if freshness is not None else None
        self._lock = threading.Lock()
        self._served: Snapshot | None = None
        self._last: Snapshot | None = None  # the last snapshot verified since the lake was new
        self._kept: set[tuple[int, dt.datetime]] = set()
        self._reason: str | None = "no snapshot has been verified yet"
        self._checked: dt.datetime | None = None

    # --- the schedule ---------------------------------------------------------------------

    def due(self, now: dt.datetime) -> bool:
        return self._checked is None or now - self._checked >= self.every

    def check(self, now: dt.datetime) -> State:
        """One verification of the lake, as the module describes; never raises for a refused
        snapshot, which only turns the state red."""
        with self._lock:
            self._checked = now
            try:
                self._check()
            except StoreError as error:
                self._served = None
                self._reason = f"the lake cannot be checked: {error.message}"
            return self._state(now)

    def _check(self) -> None:
        served = self._served
        latest = self._reads.latest()
        if served is not None and self._reads.committed_at(served.id) != served.committed_at:
            self._new("the catalog no longer lists the served snapshot with its commit time")
        elif self._last is not None and latest.id < self._last.id:
            self._new("the catalog's latest snapshot id is lower than the last one verified")
        served = self._served
        if served is not None:
            try:
                self._reads.verify(served, self._attachment())
            except StoreError as error:
                self._served = None
                self._reason = f"the served snapshot no longer passes: {error.message}"
                return
            if latest.id == served.id:
                self._reason = None
                return
        try:
            self._reads.verify(latest, self._attachment())
        except StoreError as error:
            self._reason = f"snapshot {latest.id} is held back: {error.message}"
            return
        violated = self._evidence(latest)
        if violated is not None:
            self._reason = f"snapshot {latest.id} is held back: {violated}"
            return
        self._served = self._last = latest
        self._kept.add((latest.id, latest.committed_at))
        self._reason = None

    def _new(self, reason: str) -> None:
        """The lake treated as new: nothing served, nothing verified before kept."""
        self._served = self._last = None
        self._kept.clear()
        self._reason = f"{reason}; the lake is treated as new"

    def restored(self) -> None:
        """A read found its pinned snapshot changed (`snapshot_changed`)."""
        with self._lock:
            self._new("the catalog no longer lists the snapshot read with its commit time")
            self._checked = None  # checked again at the next opportunity

    # --- what questions and receipts see ---------------------------------------------------

    def state(self, now: dt.datetime) -> State:
        with self._lock:
            return self._state(now)

    def _state(self, now: dt.datetime) -> State:
        served = self._served
        if self._reason is not None or served is None:
            return State("red", self._reason or "no snapshot is served", served)
        if self._freshness is not None and now - served.committed_at > self._freshness:
            return State(
                "amber", "the served snapshot is older than the declared freshness", served
            )
        return State("green", None, served)

    def pin(self) -> Mapping[str, object]:
        """The served snapshot, as every read of one question uses it; refused when none is."""
        with self._lock:
            served = self._served
        if served is None:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.connection}: the lake has no verified snapshot to serve.",
                {"connection": self.connection, "reason": "no_verified_snapshot"},
            )
        return {
            "id": served.id,
            "committed_at": served.committed_at.isoformat(),
            "project": self.project,
        }

    def kept(self, identity: Identity) -> bool:
        """Whether the external snapshot a receipt names is still kept: verified since the lake
        was last new, and still listed with its commit time."""
        project, connection, snapshot, committed = identity
        if (project, connection) != (self.project, self.connection):
            return False
        moment = dt.datetime.fromisoformat(committed)
        with self._lock:
            known = (snapshot, moment) in self._kept
        return known and self._reads.committed_at(snapshot) == moment
