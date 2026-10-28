"""`maintain`: expire the lake's old snapshots and compact its files, with a record (P1-20).

The project's `retention.snapshots` (default 90 days, decision #70) decides how long every
snapshot stays exactly readable: maintenance expires the snapshots committed before
`now - retention`, merges small files, and deletes the files no remaining snapshot uses, then
records what it did (`contracts/maintenance-record/v1`) in `_meta.maintenance_runs`, in its own
transaction, whether it succeeded or failed. It holds the project lock, so it never runs during
a model run. `orvex schedule` runs it once a day when no model is due.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from orvex.contracts.maintenance_record.v1 import (
    MaintenanceRecord,
    ModuleVersion,
    Report,
    RunError,
    Versions,
)
from orvex.contracts.store.v1 import (
    MaintenanceReport,
    Parameter,
    ParameterType,
    ReadRequest,
    StatementRequest,
)
from orvex.server import commands
from orvex.server.commands import LAKE_FOLDER, LOCK_FILE, Problem, project_lock
from orvex.server.identity import Identity
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, open_lake

TABLE = "_meta.maintenance_runs"
DEFAULT_RETENTION = "90d"
# An orphaned file younger than this may belong to a writer that has not committed yet.
ORPHAN_GRACE = dt.timedelta(hours=1)
# `orvex schedule`: once a day after a success, again an hour after a failure.
INTERVAL = dt.timedelta(hours=24)
RETRY = dt.timedelta(hours=1)

type Clock = Callable[[], dt.datetime]

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "VARCHAR"),
    ("trigger", "VARCHAR"),
    ("retention", "VARCHAR"),
    ("cutoff", "TIMESTAMPTZ"),
    ("versions", "VARCHAR"),
    ("report", "VARCHAR"),
    ("started_at", "TIMESTAMPTZ"),
    ("finished_at", "TIMESTAMPTZ"),
    ("outcome", "VARCHAR"),
    ("error_code", "VARCHAR"),
    ("error_message", "VARCHAR"),
)
_ISO = "'%Y-%m-%dT%H:%M:%S.%f+00:00'"


def _clock() -> dt.datetime:
    return dt.datetime.fromisoformat(commands._now())


def _text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _parameter(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter(name=name, type=ParameterType(kind), value=value)


def retention_of(project: Project) -> str:
    retention = project.settings.retention
    return (retention.snapshots if retention is not None else None) or DEFAULT_RETENTION


def record_statements(record: MaintenanceRecord) -> list[StatementRequest]:
    """Statements that create the table when missing and insert the record (bound values)."""
    columns = ", ".join(f"{name} {kind}" for name, kind in _COLUMNS)
    values: dict[str, str | None] = {
        "run_id": record.run_id,
        "trigger": record.trigger,
        "retention": record.retention,
        "cutoff": record.cutoff,
        "versions": record.versions.model_dump_json(),
        "report": None if record.report is None else record.report.model_dump_json(),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "outcome": record.outcome,
        "error_code": None if record.error is None else record.error.code,
        "error_message": None if record.error is None else record.error.message,
    }
    placeholders = ", ".join(f"$p_{name}" for name, _ in _COLUMNS)
    return [
        StatementRequest(kind="statement", sql="CREATE SCHEMA IF NOT EXISTS _meta", parameters=[]),
        StatementRequest(
            kind="statement", sql=f"CREATE TABLE IF NOT EXISTS {TABLE} ({columns})", parameters=[]
        ),
        StatementRequest(
            kind="statement",
            sql=f"INSERT INTO {TABLE} VALUES ({placeholders})",
            parameters=[_parameter(f"p_{name}", kind, values[name]) for name, kind in _COLUMNS],
        ),
    ]


def history_request(snapshot: int, time_zone: str, limit: int) -> ReadRequest:
    """Recent maintenance, newest first, with the start in `time_zone`."""
    return ReadRequest(
        kind="read",
        sql=(
            "SELECT run_id, trigger, outcome, report, error_code, "
            "strftime(timezone($p_tz, started_at), '%Y-%m-%d %H:%M:%S') AS started "
            f"FROM {TABLE} ORDER BY started_at DESC, run_id LIMIT $p_limit"
        ),
        parameters=[
            _parameter("p_tz", "VARCHAR", time_zone),
            _parameter("p_limit", "BIGINT", str(limit)),
        ],
        snapshot=snapshot,
        batch_size=1000,
    )


def last_request(snapshot: int) -> ReadRequest:
    """The end of the last maintenance that succeeded and of the last that failed (UTC)."""
    return ReadRequest(
        kind="read",
        sql=(
            f"SELECT strftime(timezone('UTC', max(finished_at) FILTER (WHERE outcome = "
            f"'succeeded')), {_ISO}) AS succeeded, "
            f"strftime(timezone('UTC', max(finished_at) FILTER (WHERE outcome = 'failed')), "
            f"{_ISO}) AS failed FROM {TABLE}"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )


@dataclass(frozen=True, slots=True)
class Last:
    """When maintenance last ended, by outcome (None: never)."""

    succeeded: dt.datetime | None = None
    failed: dt.datetime | None = None

    def due_at(self) -> dt.datetime | None:
        """When the scheduler should run maintenance next (None: now)."""
        if self.succeeded is None and self.failed is None:
            return None
        if self.failed is not None and (self.succeeded is None or self.failed > self.succeeded):
            return self.failed + RETRY
        assert self.succeeded is not None
        return self.succeeded + INTERVAL


def last_maintenance(lake: Lake) -> Last:
    """What the records say (nothing yet when there is no record)."""
    snapshot = lake.current_snapshot().id
    try:
        (row,) = lake.read(last_request(snapshot)).read_all().to_pylist()
    except StoreError as error:
        if error.code != "STORE_STATEMENT_INVALID":
            raise
        return Last()  # the records table does not exist yet

    def instant(value: object) -> dt.datetime | None:
        return None if value is None else dt.datetime.fromisoformat(str(value))

    return Last(instant(row["succeeded"]), instant(row["failed"]))


def _record(
    run_id: str,
    trigger: Literal["manual", "schedule"],
    retention: str,
    cutoff: dt.datetime,
    versions: Versions,
    started: dt.datetime,
    finished: dt.datetime,
    report: MaintenanceReport | None,
    error: StoreError | None,
) -> MaintenanceRecord:
    return MaintenanceRecord(
        run_id=run_id,
        trigger=trigger,
        retention=retention,
        cutoff=_text(cutoff),
        versions=versions,
        report=None
        if report is None
        else Report(
            snapshots_expired=report.snapshots_expired,
            files_merged=report.files_merged,
            files_created=report.files_created,
            files_rewritten=report.files_rewritten,
            files_deleted=report.files_deleted,
            orphaned_files_deleted=report.orphaned_files_deleted,
            oldest_snapshot_id=report.oldest_snapshot.id,
            oldest_snapshot_at=_text(report.oldest_snapshot.committed_at),
        ),
        started_at=_text(started),
        finished_at=_text(finished),
        outcome="succeeded" if error is None else "failed",
        error=None if error is None else RunError(code=error.code, message=error.message),
    )


def run_maintenance(
    project: Project,
    lake: Lake,
    identity: Identity,
    trigger: Literal["manual", "schedule"],
    clock: Clock,
) -> MaintenanceRecord:
    """Maintain an open lake and record the outcome; the caller holds the project lock."""
    retention = retention_of(project)
    started = clock()
    cutoff = started - dt.timedelta(days=int(retention[:-1]))
    report: MaintenanceReport | None = None
    failure: StoreError | None = None
    try:
        report = lake.maintain(cutoff, started - ORPHAN_GRACE)
    except StoreError as error:
        failure = error
    commanded = commands._versions(identity)
    versions = Versions(
        duckdb=commanded.duckdb,
        modules=[ModuleVersion(name=m.name, version=m.version) for m in commanded.modules],
    )
    record = _record(
        str(uuid.uuid4()),
        trigger,
        retention,
        cutoff,
        versions,
        started,
        clock(),
        report,
        failure,
    )
    with lake.transaction() as transaction:
        for statement in record_statements(record):
            transaction.execute(statement)
    return record


def summary(record: MaintenanceRecord) -> str:
    """One line: what a maintenance did, or why it failed."""
    if record.report is None:
        assert record.error is not None
        return f"maintenance failed: {record.error.code} {record.error.message}"
    report = record.report
    return (
        f"maintenance done: {report.snapshots_expired} snapshots expired "
        f"(retention {record.retention}), {report.files_merged} files merged into "
        f"{report.files_created}, {report.files_rewritten} rewritten, {report.files_deleted} "
        f"deleted, {report.orphaned_files_deleted} orphaned deleted; oldest snapshot kept: "
        f"{report.oldest_snapshot_id}"
    )


def _load(folder: Path, out: IO[str]) -> Project | None:
    try:
        return load_project(folder)
    except ProjectLoadError as error:
        details = error.error.details
        print(Problem(error.code, details.file or ".", details.line, error.error.message), file=out)
        return None


def _locked(identity: Identity, out: IO[str], what: str) -> int:
    print(
        Problem("PROJECT_LOCKED", f".{identity.slug}/{LOCK_FILE}", None, what),
        file=out,
    )
    return 1


def maintain(
    folder: Path,
    identity: Identity,
    out: IO[str],
    trigger: Literal["manual", "schedule"] = "manual",
    clock: Clock = _clock,
) -> int:
    """Expire snapshots older than the project's retention and compact the lake's files."""
    project = _load(folder, out)
    if project is None:
        return 1
    home = project.folder / f".{identity.slug}"
    if not (home / LAKE_FOLDER).exists():
        print("nothing to maintain: the project has no lake yet", file=out)
        return 0
    try:
        with project_lock(home):
            lake = open_lake(commands._location(home), commands._limits())
            try:
                record = run_maintenance(project, lake, identity, trigger, clock)
            finally:
                lake.close()
    except commands.ProjectLocked:
        return _locked(identity, out, "another process is running this project; try again later.")
    except StoreError as error:
        print(commands._store_problem(error, ".", ""), file=out)
        return 1
    if record.error is not None:
        print(
            Problem(record.error.code, ".", None, f"lake maintenance: {record.error.message}"),
            file=out,
        )
        return 1
    print(summary(record), file=out)
    return 0


def history(folder: Path, identity: Identity, limit: int, out: IO[str]) -> int:
    """Recent maintenance of a project, newest first, with times in the project's time zone."""
    project = _load(folder, out)
    if project is None:
        return 1
    home = project.folder / f".{identity.slug}"
    rows: list[dict[str, object]] = []
    if (home / LAKE_FOLDER).exists():
        try:
            with project_lock(home):
                lake = open_lake(commands._location(home), commands._limits())
                try:
                    snapshot = lake.current_snapshot().id
                    request = history_request(snapshot, project.settings.time_zone, limit)
                    try:
                        rows = lake.read(request).read_all().to_pylist()
                    except StoreError as error:
                        if error.code != "STORE_STATEMENT_INVALID":
                            raise
                finally:
                    lake.close()
        except commands.ProjectLocked:
            return _locked(identity, out, "a run is in progress.")
        except StoreError as error:
            print(commands._store_problem(error, ".", ""), file=out)
            return 1
    if not rows:
        print("no maintenance yet", file=out)
    for row in rows:
        detail = row["error_code"] or ""
        if row["report"] is not None:
            report = Report.model_validate_json(str(row["report"]))
            detail = (
                f"{report.snapshots_expired} expired, {report.files_merged} merged, "
                f"{report.files_deleted} deleted, oldest snapshot {report.oldest_snapshot_id}"
            )
        print(
            f"{row['run_id']}  {row['started']}  {row['outcome']:<9}  {row['trigger']:<8}  "
            f"{detail}",
            file=out,
        )
    return 0
