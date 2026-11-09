"""What a run records about itself: the clock, the project's commit, the versions, each model's
outcome and the run records built from them."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from orvex.contracts.run_record.v1 import (
    CheckResult,
    ModuleVersion,
    RowsRead,
    RowsWritten,
    RunError,
    RunRecord,
    Versions,
)
from orvex.server.identity import Identity
from orvex.server.versions import module_versions
from orvex.transform.models import Model

if TYPE_CHECKING:
    from orvex.server.run import Selection


GIT_TIMEOUT_S = 10


def _git(folder: Path, *arguments: str) -> str | None:
    """Output of a git command in `folder`, or None when git or the repository is missing."""
    try:
        result = subprocess.run(
            # No fsmonitor hook and no index rewrite: reading the commit must not run the
            # project's configured programs or change its files.
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "--no-optional-locks",
                "-C",
                str(folder),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 else None


def _project_commit(folder: Path) -> tuple[str | None, bool | None]:
    commit = _git(folder, "rev-parse", "HEAD")
    if commit is None or not commit.strip():
        return None, None
    status = _git(
        folder, "status", "--porcelain", "--", "models", "project.yaml", "connections.yaml"
    )
    return commit.strip(), None if status is None else bool(status.strip())


def _versions(identity: Identity) -> Versions:
    return Versions(
        duckdb=importlib.metadata.version("duckdb"),
        modules=[
            ModuleVersion(name=name, version=number) for name, number in module_versions(identity)
        ],
    )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")


@dataclass(slots=True)
class _Outcome:
    model: Model
    started_at: str
    finished_at: str
    outcome: str = "not_run"
    rows_read: str | None = None
    rows_written: str | None = None
    total: str | None = None  # an incremental table's rows after the run (#257)
    error: RunError | None = None
    since: str | None = None
    watermark: str | None = None
    tests: list[CheckResult] = field(default_factory=list)
    reconciliation: list[CheckResult] = field(default_factory=list)
    started: bool = False
    lineage_run_id: str = field(default_factory=lambda: str(uuid.uuid7()))


def _trigger(
    name: str, refreshed: frozenset[str], selection: Selection | None
) -> Literal["manual", "schedule", "freshness", "full_refresh"]:
    if name in refreshed or (selection is not None and name in selection.full_refresh):
        return "full_refresh"
    return "manual" if selection is None else selection.trigger


def _records(
    outcomes: Sequence[_Outcome],
    run_id: str,
    commit: str | None,
    dirty: bool | None,
    identity: Identity,
    refreshed: frozenset[str],
    selection: Selection | None,
) -> list[RunRecord]:
    """One record per model. `snapshot_id` is not stored: it is the snapshot the records
    commit in, resolved when they are read (#394)."""
    return [
        RunRecord(
            run_id=run_id,
            model=item.model.name,
            trigger=_trigger(item.model.name, refreshed, selection),
            materialized=item.model.config.materialized or "table",
            project_commit=commit,
            project_dirty=dirty,
            versions=_versions(identity),
            since=item.since,
            rows_read=None if item.rows_read is None else RowsRead(item.rows_read),
            rows_written=None
            if item.rows_written is None or item.outcome != "success"
            else RowsWritten(item.rows_written),
            watermark=item.watermark if item.outcome == "success" else None,
            tests=item.tests,
            reconciliation=item.reconciliation,
            snapshot_id=None,
            started_at=item.started_at,
            finished_at=item.finished_at,
            outcome=item.outcome,  # type: ignore[arg-type]  # success, failed or not_run
            error=item.error,
        )
        for item in outcomes
    ]
