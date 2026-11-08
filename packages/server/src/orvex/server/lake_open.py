"""Finding and opening the project's lake: its place, its limits, an existing lake, and a
run's first open with its record."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from pathlib import Path

from orvex.server import lake_place
from orvex.server.lake_place import Place
from orvex.server.project import Project, ProjectLoadError
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake
from orvex.transform.sources import Attachment

DEFAULT_MEMORY = "4GB"
DEFAULT_TIMEOUT_MS = 3_600_000


def lake_location(project: Project, home: Path) -> Place:
    """Where the project's lake is: the configured catalog, checked against the one the
    existing lake was created with (#120). Nothing is opened or created."""
    chosen = lake_place.place(project, home)
    lake_place.check_unchanged(home, chosen)
    return chosen


def memory_limit() -> str:
    """Three quarters of the machine's memory, or 4GB when it cannot be read."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except AttributeError, ValueError, OSError:
        return DEFAULT_MEMORY
    return f"{max(1, total * 3 // 4 // (1024 * 1024))}MB"


def _open_recorded(home: Path, place: Place, attachments: Sequence[Attachment] = ()) -> Lake:
    """Open the project's lake for a run, its record written first (`creating`) and confirmed
    once it opened, so no lake is ever left without a record (#146). A failed first open drops
    its record when nothing was created."""
    _refuse_missing_catalog(home)
    lake_place.begin(home, place)
    try:
        lake = open_lake(place.location, _limits(), attachments)
    except BaseException:
        # Ctrl-C or any error: the record goes only when nothing was created (#181). A record
        # that cannot be removed stays `creating`, which binds nothing that was not created; the
        # open's own error is the one reported (#192).
        with contextlib.suppress(ProjectLoadError):
            lake_place.abandon(home, place)
        raise
    try:
        lake_place.confirm(home, place)
        lake_place.complete(home, place)  # a record written before #147 gets its endpoint
    except BaseException:
        lake.close()
        raise
    return lake


def open_existing(home: Path, place: Place, memory: str | None = None) -> Lake:
    """Open the project's existing lake (with `memory`, its share), under the project lock, after
    checking again that its settings are the lake's own: another process may have created the
    lake since the project was checked (#181, Reviewer pass 2). `ProjectLoadError` otherwise."""
    lake_place.check_unchanged(home, place)
    _refuse_missing_catalog(home)
    lake_place.adopt(home, place)  # a default lake made before #204 gets its record
    lake = open_lake(place.location, _limits(memory))
    try:
        lake_place.complete(home, place)  # a record written before #147 gets its endpoint
    except BaseException:
        lake.close()
        raise
    return lake


def _refuse_missing_catalog(home: Path) -> None:
    """`STORE_CATALOG_UNAVAILABLE` when the project's confirmed lake has lost its DuckDB or
    SQLite catalog file: opening the place again would create a new, empty lake there and hide
    its history (#192). Nothing is opened or created."""
    missing = lake_place.missing_catalog(home)
    data = lake_place.orphan_data(home)
    if missing is not None and data is not None:
        raise StoreError(
            "STORE_CATALOG_UNAVAILABLE",
            f"The lake's catalog file {missing} is missing, but the data folder {data} holds "
            "data files of an earlier lake. The catalog holds the lake's history (runs, "
            "watermarks, snapshots), so a new one is never created next to them: restore the "
            f"file, or move {home.name}/{lake_place.LAKE_FOLDER} away (to keep the data files) "
            "to start a new, empty lake.",
            {"path": missing, "data": data},
        )
    if missing is not None:
        raise StoreError(
            "STORE_CATALOG_UNAVAILABLE",
            f"The lake's catalog file {missing} is missing. It holds the lake's history (runs, "
            "watermarks, snapshots), so a new one is never created in its place: restore the "
            f"file (or connect the disk it is on), or remove {home.name}/{lake_place.RECORD_FILE} "
            f"and {home.name}/{lake_place.LAKE_FOLDER} (move them away to keep the data files) "
            "to start a new, empty lake.",
            {"path": missing},
        )


def _limits(memory: str | None = None) -> Limits:
    return Limits(
        memory_limit=memory or memory_limit(),
        threads=os.cpu_count() or 2,
        max_concurrent=4,
        queue_size=16,
        default_timeout_ms=DEFAULT_TIMEOUT_MS,
    )
