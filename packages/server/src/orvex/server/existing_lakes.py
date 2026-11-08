"""The served project's existing DuckLakes in the `serve` process (P2-30 PR 3, #399; ADR-0027
§3, §5, §7, §8).

With the development switch on (`development_switch`), every `ducklake` connection of the served
project is attached when the server starts: a DuckDB instance of its own per lake
(`orvex.store.existing_lake`), its guarded reads, its verified-snapshot schedule
(`existing_lake_snapshots`) and its adapter (`existing_lake_adapter`). With the switch off, none
is, and a dataset on one is `UNSUPPORTED_OPERATION` (`operation: route`).

- **One memory budget.** The managed lake's instance and every lake's instance share the
  instance's memory budget in equal parts (`memory_share`): their limits together never exceed
  it. Questions and checks take the project's one turn (`semantic_http.ProjectSource`), the
  serve process's admission control.
- **The schedule runs here.** Every lake is checked once before the server answers, then
  whenever its schedule is due, by one background thread (`checking`). A check holds the turn,
  so a question never sees a lake halfway through one.
- **Questions** (`pinned`): each lake's tables and views at its served snapshot, with its adapter
  pinned at that snapshot, so the model is compiled and read at one snapshot. A lake with
  nothing served is the `StoreError` saying so, which the model reports as its problem.
- **Evidence of a new snapshot:** the model is compiled with that lake's tables at the new
  snapshot, and its datasets' evidence is read there (`existing_lake_evidence`). Another lake
  with nothing served contributes its latest tables to that compilation only (catalog metadata,
  never its data), so two lakes never wait on each other. A model that cannot be compiled, or a
  check that cannot have the project's turn, holds the snapshot back.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from orvex.contracts.project.v1 import DucklakeConnection
from orvex.contracts.store.v1 import ExistingLakeAttachment, Snapshot
from orvex.server import existing_lake_attachments
from orvex.server.existing_lake_adapter import ExistingLakeAdapter
from orvex.server.existing_lake_evidence import violated
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.server.project import ProjectLoadError
from orvex.server.semantic_http import ProjectBusy, ProjectSource, Refused
from orvex.server.semantic_model import ExternalTables
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import LakeReads
from orvex.store.lake import Lake

log = logging.getLogger(__name__)
POLL_SECONDS = 30.0  # how often the thread looks for a due schedule (each is at least 5m)
_SIZE = re.compile(r"([0-9]+)\s*(MB|GB|TB)", re.IGNORECASE)
_MB = {"mb": 1, "gb": 1024, "tb": 1024 * 1024}


def memory_share(total: str, instances: int) -> str:
    """One instance's part of the memory budget `total` (`<n>MB`, `<n>GB` or `<n>TB`) shared
    equally by `instances`."""
    found = _SIZE.fullmatch(total.strip())
    if found is None or instances < 1:
        raise ValueError(f"not a memory budget to share: {total!r} by {instances}")
    megabytes = int(found.group(1)) * _MB[found.group(2).lower()]
    return f"{max(1, megabytes // instances)}MB"


def ducklake_connections(connections: Mapping[str, object]) -> dict[str, DucklakeConnection]:
    return {n: c for n, c in connections.items() if isinstance(c, DucklakeConnection)}


@dataclass(frozen=True, slots=True)
class _Lake:
    reads: LakeReads
    schedule: SnapshotSchedule
    adapter: ExistingLakeAdapter


class ExistingLakes:
    """The attached lakes of one served project, by connection name."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._lakes: dict[str, _Lake] = {}
        self._opened: list[ExistingLake] = []
        # During a check: the served project, and its managed lake or why it could not be had.
        self._source: ProjectSource | None = None
        self._held: Lake | str | None = None

    def __len__(self) -> int:
        return len(self._lakes)

    def add(
        self,
        name: str,
        lake: ExistingLake,
        schedule: Callable[[Callable[[Snapshot], str | None]], SnapshotSchedule],
        reads: LakeReads,
        engine_version: str,
    ) -> None:
        """`lake`, its reads, and its schedule made with this composition's evidence."""
        self._opened.append(lake)
        made = schedule(lambda snapshot: self._evidence(name, snapshot))
        self._lakes[name] = _Lake(reads, made, ExistingLakeAdapter(reads, made, engine_version))

    def schedule(self, name: str) -> SnapshotSchedule:
        return self._lakes[name].schedule

    # --- questions -------------------------------------------------------------------------

    def pinned(self) -> dict[str, ExternalTables | StoreError]:
        """For one question: every lake at its served snapshot, or why it has none."""
        return self._external(None)

    def _external(
        self, candidate: tuple[str, Snapshot] | None
    ) -> dict[str, ExternalTables | StoreError]:
        found: dict[str, ExternalTables | StoreError] = {}
        for name, entry in self._lakes.items():
            adapter: ExistingLakeAdapter | None = None
            try:
                if candidate is not None and candidate[0] == name:
                    snapshot = candidate[1]
                else:
                    try:
                        adapter = entry.adapter.fixed()
                        snapshot = adapter.snapshot()
                    except StoreError:
                        if candidate is None:
                            raise
                        snapshot = entry.reads.latest()  # for compiling only: never read
                found[name] = ExternalTables(
                    entry.reads.table_columns(snapshot), entry.reads.views(snapshot), adapter
                )
            except StoreError as error:
                found[name] = error
        return found

    # --- the schedule ----------------------------------------------------------------------

    def check(self, source: ProjectSource) -> None:
        """Every due lake checked, holding the project's turn when it can be had."""
        now = self._now()
        due = [(name, e) for name, e in self._lakes.items() if e.schedule.due(now)]
        if not due:
            return
        with ExitStack() as stack:
            self._source = source
            try:
                self._held = stack.enter_context(source.session())
            except ProjectBusy:
                self._held = "a run holds the project's lake"
            except Refused as error:
                self._held = error.message
            except (StoreError, ProjectLoadError) as error:
                self._held = str(error)
            try:
                for name, entry in due:
                    state = entry.schedule.check(now)
                    if state.color != "green":
                        log.warning(
                            "existing lake not green",
                            extra={"connection": name, "color": state.color, "why": state.reason},
                        )
            finally:
                self._source, self._held = None, None

    def _evidence(self, name: str, snapshot: Snapshot) -> str | None:
        """What the new `snapshot` of lake `name` violates of the model's evidence, or None."""
        held, source = self._held, self._source
        if not isinstance(held, Lake) or source is None:
            return f"its evidence cannot be checked: {held or 'no check is running'}"
        loaded = source.model(
            held, held.current_snapshot().id, external=self._external((name, snapshot))
        )
        if loaded.model is None:
            if not loaded.problems:
                return None  # no semantic model: no evidence is declared
            codes = ", ".join(sorted({problem.code for problem in loaded.problems}))
            return f"the semantic model cannot be compiled on it ({codes})"
        reads = self._lakes[name].reads
        return violated(
            loaded.model.compiled,
            loaded.model.routing,
            name,
            lambda request: reads.read(request, snapshot.committed_at),
            snapshot,
            self._now(),
        )

    @contextmanager
    def checking(self, source: ProjectSource, poll: float = POLL_SECONDS) -> Iterator[None]:
        """Every lake checked now, then whenever due, until the block ends."""
        self.check(source)
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(poll):
                try:
                    self.check(source)
                except Exception:  # the thread must outlive one failed check, which is logged
                    log.exception("existing lake check failed")

        thread = threading.Thread(target=loop, name="existing-lakes", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def close(self) -> None:
        for lake in self._opened:
            lake.close()


@contextmanager
def opened(
    folder: Path,
    directory: Path,
    slug: str,
    project: str,
    connections: Mapping[str, object],
    *,
    memory_limit: str,
    threads: int,
    timeout_ms: int,
    engine_version: str,
) -> Iterator[ExistingLakes]:
    """Every `ducklake` connection of the project at `folder` attached, each instance with
    `memory_limit` (its share); all closed when the block ends. A lake that cannot be attached
    is the error, and none stays open."""
    lakes = ExistingLakes()
    try:
        for name, connection in sorted(ducklake_connections(connections).items()):
            current = existing_lake_attachments.current(directory, folder, slug, name, connection)
            lake = ExistingLake(current(), memory_limit=memory_limit, threads=threads)
            reads = LakeReads(lake, timeout_ms=timeout_ms)

            def schedule(
                evidence: Callable[[Snapshot], str | None],
                name: str = name,
                connection: DucklakeConnection = connection,
                reads: LakeReads = reads,
                current: Callable[[], ExistingLakeAttachment] = current,
            ) -> SnapshotSchedule:
                return SnapshotSchedule(
                    project,
                    name,
                    reads,
                    attachment=current,
                    evidence=evidence,
                    schedule=connection.schedule,
                    freshness=connection.freshness,
                )

            lakes.add(name, lake, schedule, reads, engine_version)
        yield lakes
    finally:
        lakes.close()
