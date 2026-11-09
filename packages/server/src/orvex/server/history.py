"""`runs`: a project's recent runs, newest first, from its run records."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import IO

from orvex.server import heartbeat, lake_open, lake_place, run_metadata
from orvex.server.error_problems import _load_problem, _store_problem
from orvex.server.identity import Identity
from orvex.server.lake_open import lake_location
from orvex.server.locking import LOCK_FILE, ProjectLocked, project_lock
from orvex.server.problems import Problem
from orvex.server.project import ProjectLoadError, load_project
from orvex.store.errors import StoreError
from orvex.transform.run_records import history_request


def runs(folder: Path, identity: Identity, limit: int, out: IO[str]) -> int:
    """Recent runs of a project, newest first, with times in the project's time zone."""
    try:
        project = load_project(folder)
        home = project.folder / f".{identity.slug}"
        place = lake_location(project, home)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    zone = project.settings.time_zone
    print(heartbeat.describe(home, zone, dt.datetime.fromisoformat(run_metadata._now())), file=out)
    if not lake_place.exists(home):
        print("no runs yet", file=out)
        return 0
    try:
        with project_lock(home):
            lake = lake_open.open_existing(home, place)
            try:
                # The tables before the snapshot: record tables are never dropped, so each one
                # listed exists at the snapshot read after them (#394).
                tables = lake.tables()
                snapshot = lake.current_snapshot().id
                request = history_request(snapshot, zone, limit, tables)
                # No request: the lake has no record table yet.
                rows = [] if request is None else lake.read(request).read_all().to_pylist()
            finally:
                lake.close()
    except ProjectLocked:
        print(
            Problem(
                "PROJECT_LOCKED", f".{identity.slug}/{LOCK_FILE}", None, "a run is in progress."
            ),
            file=out,
        )
        return 1
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    except StoreError as error:
        print(_store_problem(error, ".", ""), file=out)
        return 1
    if not rows:
        print("no runs yet", file=out)
    for row in rows:
        print(
            f"{row['run_id']}  {row['started']}  {row['outcome']:<7}  "
            f"snapshot {row['snapshot'] if row['snapshot'] is not None else '-'}  {row['models']}",
            file=out,
        )
    return 0
