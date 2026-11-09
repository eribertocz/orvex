"""`run`: build every model of a project, or a scheduled selection, into its lake in one write
transaction, and record the run.

Transform hands the server the run's statements; they run in one write transaction, with the
run records, so a stop at any point leaves both or neither.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from orvex.contracts.run_record.v1 import RunError, RunRecord
from orvex.server import lake_place, project_check, run_metadata
from orvex.server.error_problems import _load_problem, _store_message, _store_problem
from orvex.server.evidence_command import _evidence_after_run
from orvex.server.identity import Identity
from orvex.server.lake_open import _open_recorded
from orvex.server.leftovers import _drop_statements, _leftovers
from orvex.server.lineage import _lineage_events, _publish_lineage
from orvex.server.locking import LOCK_FILE, ProjectLocked, project_lock
from orvex.server.problems import Problem
from orvex.server.project import Project, ProjectLoadError
from orvex.server.project_check import Checked, databases
from orvex.server.run_metadata import _Outcome, _project_commit, _records
from orvex.server.run_reads import _committed_snapshot, _count, _since, _table_columns
from orvex.server.run_steps import _check_tests, _run_step, _StepFailed
from orvex.store.errors import StoreError
from orvex.store.lake import Transaction
from orvex.transform import lineage
from orvex.transform.graph import Graph, plan_run
from orvex.transform.incremental import Columns
from orvex.transform.run_records import record_statements
from orvex.transform.sources import source_attachments


class RunControl:
    """Cancels a run from another thread (the scheduler's stop, #69): the run's transaction is
    cancelled, its running statement interrupted, and nothing of it is committed. A cancel that
    comes before the transaction opens cancels it as soon as it does."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transaction: Transaction | None = None
        self._cancelled = False

    def attach(self, transaction: Transaction) -> None:
        with self._lock:
            self._transaction = transaction
            cancelled = self._cancelled
        if cancelled:
            transaction.cancel()

    def detach(self) -> None:
        with self._lock:
            self._transaction = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            transaction = self._transaction
        if transaction is not None:
            transaction.cancel()


@dataclass(frozen=True, slots=True)
class Selection:
    """A scheduled run: the models to build (dependencies first) and how to record it."""

    models: tuple[str, ...]
    trigger: Literal["freshness", "schedule"]
    full_refresh: frozenset[str] = frozenset()  # recorded as full_refresh (incremental: rebuilt)
    control: RunControl | None = None  # lets the scheduler cancel the run when it stops


def run(
    folder: Path,
    identity: Identity,
    out: IO[str],
    full_refresh: Sequence[str] | None = None,
    selection: Selection | None = None,
    drop_leftovers: bool = False,
) -> int:
    """Build every model, or only the `selection` of a scheduled run. `full_refresh`:
    incremental models to rebuild from every row (all of them when empty; None: none).
    `drop_leftovers`: also drop, in the run's transaction, the lake tables no model stores
    now (#88, decision #233); never in a scheduled run."""
    if drop_leftovers and selection is not None:
        print(
            Problem(
                "RUN_OPTION_INVALID",
                ".",
                None,
                "--drop-leftovers is a manual step; a scheduled run never drops tables.",
            ),
            file=out,
        )
        return 1
    checked = project_check.check_project(folder, identity)
    if checked.problems or checked.project is None or checked.graph is None:
        for problem in checked.problems:
            print(problem, file=out)
        return 1
    project, graph = checked.project, checked.graph
    incremental_names = {
        m.name for m in checked.discovery.models if m.config.materialized == "incremental"
    }
    refreshed: frozenset[str] = frozenset()
    if full_refresh is not None:
        unknown = sorted(set(full_refresh) - incremental_names)
        if unknown:
            for name in unknown:
                print(
                    Problem(
                        "MODEL_REFERENCE_UNKNOWN",
                        ".",
                        None,
                        f"--full-refresh {name}: not an incremental model of the project.",
                    ),
                    file=out,
                )
            return 1
        refreshed = frozenset(full_refresh or incremental_names)
    if selection is not None:
        refreshed = selection.full_refresh & incremental_names
    home = project.folder / f".{identity.slug}"
    try:
        with project_lock(home):
            return _run_locked(
                project, graph, checked, home, identity, out, refreshed, selection, drop_leftovers
            )
    except ProjectLocked:
        print(
            Problem(
                "PROJECT_LOCKED",
                f".{identity.slug}/{LOCK_FILE}",
                None,
                "another process is running this project; try again when it ends.",
            ),
            file=out,
        )
        return 1


@contextlib.contextmanager
def _controlled(control: RunControl | None, transaction: Transaction) -> Iterator[None]:
    """While the block runs, `control` can cancel `transaction`."""
    if control is None:
        yield
        return
    control.attach(transaction)
    try:
        yield
    finally:
        control.detach()


def _run_locked(
    project: Project,
    graph: Graph,
    checked: Checked,
    home: Path,
    identity: Identity,
    out: IO[str],
    refreshed: frozenset[str] = frozenset(),
    selection: Selection | None = None,
    drop_leftovers: bool = False,
) -> int:
    discovery = checked.discovery
    run_id = str(uuid.uuid4())
    print(f"run {run_id}", file=out)
    commit, dirty = _project_commit(project.folder)
    started = run_metadata._now()
    snapshot: int | None = None
    failures: list[Problem] = []
    lake = None
    open_error: StoreError | None = None
    assert checked.place is not None  # a refused place is a problem: no run
    place = checked.place
    try:
        # Checked again under the project lock: another process may have started this lake's
        # first open since the project was checked (#181, Reviewer pass 1).
        lake_place.check_unchanged(home, place)
    except ProjectLoadError as error:
        print(_load_problem(error), file=out)
        return 1
    try:
        lake = _open_recorded(home, place, source_attachments(project.sources))
    except StoreError as error:
        open_error = error
    except ProjectLoadError as error:  # the lake record cannot be written (#192)
        print(_load_problem(error), file=out)
        return 1
    existing: dict[str, Columns | None] = {}
    previous: dict[str, str | None] = {}
    since: dict[str, str] = {}
    leftovers: list[tuple[str, str]] = []
    if lake is not None:
        try:
            leftovers = _leftovers(lake, discovery.models)
            for model in discovery.models:
                if model.config.materialized == "incremental":
                    existing[model.name] = _table_columns(lake, model)
                    previous[model.name], since[model.name] = _since(
                        lake,
                        model,
                        checked.columns.get(model.name, []),
                        existing[model.name],
                        model.name in refreshed,
                    )
        except StoreError as error:
            lake.close()
            lake, open_error = None, error
    plan = plan_run(
        discovery.models,
        graph,
        checked.columns,
        existing,
        since,
        refreshed,
        previous,
        checked.differences,
        checked.filtered,
        databases(project),
    )
    if selection is not None:
        plan = [(model, steps) for model, steps in plan if model.name in selection.models]
    outcomes = [
        _Outcome(
            model,
            started,
            started,
            since=since.get(model.name),
            watermark=previous.get(model.name),
        )
        for model, _ in plan
    ]
    if open_error is not None:
        failures = [_store_problem(open_error, ".", "")]
        for item in outcomes:
            item.outcome = "failed"
            item.error = RunError(code=open_error.code, message=open_error.message)
    if lake is None:
        # The run could not start (a source is unreachable, say): it is still recorded, in a
        # lake opened without sources.
        try:
            lake = _open_recorded(home, place)
        except StoreError as error:
            for problem in failures or [_store_problem(error, ".", "")]:
                print(problem, file=out)
            return 1
        except ProjectLoadError as error:  # the lake record cannot be written (#192)
            print(_load_problem(error), file=out)
            return 1
    interrupted = False
    recorded = False  # the success records committed with the run's data (#394)
    warning: Problem | None = None

    def records() -> list[RunRecord]:
        return _records(outcomes, run_id, commit, dirty, identity, refreshed, selection)

    try:
        current: _Outcome | None = None
        finished = False  # the block ran to its end: what fails after it is the commit
        try:
            control = selection.control if selection is not None else None
            with lake.transaction() as transaction, _controlled(control, transaction):
                for item, (_, steps) in zip(outcomes, plan, strict=True):
                    if failures:
                        break
                    current = item
                    item.started_at = run_metadata._now()
                    item.started = True
                    for step in steps:
                        _run_step(transaction, step, item)
                    _check_tests(item)
                    item.finished_at = run_metadata._now()
                    item.outcome = "ran"
                current = None
                if failures:
                    raise _NothingToCommit
                if drop_leftovers:
                    for statement in _drop_statements(leftovers):
                        transaction.execute(statement)
                # The records commit with the data, so a stop at any point leaves both or
                # neither (#394). Rows are counted here, in the transaction that wrote them.
                for item in outcomes:
                    if item.rows_written is None and item.model.config.materialized != "view":
                        item.rows_written = _count(transaction, item.model)
                    if item.model.config.materialized == "incremental":
                        item.total = _count(transaction, item.model)
                    item.outcome = "success"
                for statement in record_statements(records()):
                    transaction.execute(statement)
                finished = True
            recorded = True
        except _NothingToCommit:
            pass
        except (StoreError, _StepFailed, KeyboardInterrupt) as error:
            # Ctrl-C: the run is recorded, then the interruption goes on.
            interrupted = isinstance(error, KeyboardInterrupt)
            committed = _committed_snapshot(lake, run_id) if finished else None
            if committed is not None:
                # Stopped after the commit (the token lookup, say): the run and its records
                # are committed, so no failed record is added.
                recorded, snapshot = True, committed
                if isinstance(error, StoreError):
                    warning = _store_problem(error, ".", "after the commit")
            else:
                failures = _rolled_back(error, outcomes, current)
        if recorded and snapshot is None:
            # The snapshot this run committed, found by its token, never the latest one:
            # another clone sharing the lake may have committed since (#255).
            result = transaction.result
            if result is not None and result.snapshot is not None:
                snapshot = result.snapshot.id
            else:
                warning = _store_problem(
                    StoreError(
                        "STORE_SNAPSHOT_NOT_FOUND", "The run's snapshot could not be read.", {}
                    ),
                    ".",
                    "after the commit",
                )
        events: list[lineage.Event] = []
        lineage_failure: Problem | None = None
        try:
            events = _lineage_events(project, identity, run_id, outcomes, checked, lake)
        except Exception as error:  # noqa: BLE001 (lineage must never change the run's outcome)
            lineage_failure = Problem(
                "LINEAGE_NOT_SENT",
                ".",
                None,
                f"lineage events could not be built ({type(error).__name__}).",
            )
        records_error: StoreError | None = None
        if not recorded:
            # Nothing of the run committed: it is recorded in a transaction of its own.
            try:
                with lake.transaction() as transaction:
                    for statement in record_statements(records()):
                        transaction.execute(statement)
            except StoreError as error:
                records_error = error
        evidence: list[str] = []
        if not failures and not interrupted and records_error is None and snapshot is not None:
            written = [
                item.model.name
                for item in outcomes
                if item.outcome == "success" and item.model.config.materialized != "view"
            ]
            evidence = _evidence_after_run(project, identity, lake, snapshot, written, run_id)
    finally:
        lake.close()
    if interrupted:
        for problem in failures:
            print(problem, file=out)
        if records_error is not None:
            print(_store_problem(records_error, ".", "run records were not written"), file=out)
        raise KeyboardInterrupt
    lineage_warning = lineage_failure or _publish_lineage(project, home, run_id, events)
    if records_error is not None:
        for problem in failures:
            print(problem, file=out)
        print(_store_problem(records_error, ".", "run records were not written"), file=out)
        if lineage_warning is not None:
            print(lineage_warning, file=out)
        return 1
    if failures:
        for problem in failures:
            print(problem, file=out)
        if lineage_warning is not None:
            print(lineage_warning, file=out)
        return 1
    for notice in (warning, lineage_warning):
        if notice is not None:
            print(notice, file=out)
    print(f"snapshot {snapshot if snapshot is not None else '-'}", file=out)
    for line in evidence:
        print(line, file=out)
    for item in outcomes:
        if item.model.config.materialized == "view":
            print(f"tested {item.model.name}", file=out)  # a view is not stored
        else:
            rows = "unknown" if item.rows_written is None else item.rows_written
            if item.model.config.materialized == "incremental":
                # New rows, and the table they joined (#257): 0 new rows is not an empty table.
                total = "table total unknown" if item.total is None else f"{item.total} in table"
                print(f"built {item.model.name}: {rows} new rows ({total})", file=out)
            else:
                print(f"built {item.model.name}: {rows} rows", file=out)
    for schema, table in leftovers:
        if drop_leftovers:
            print(f"dropped {schema}.{table}", file=out)
        else:
            print(
                f"leftover {schema}.{table}: no model stores it; kept as it was "
                "(remove it with --drop-leftovers)",
                file=out,
            )
    return 0


class _NothingToCommit(Exception):
    """Leaves the run transaction without changing anything (the run could not start)."""


def _rolled_back(
    error: StoreError | _StepFailed | KeyboardInterrupt,
    outcomes: Sequence[_Outcome],
    current: _Outcome | None,
) -> list[Problem]:
    """Mark the models of a run that rolled back, with the reason; the run's problems."""
    for item in outcomes:
        if item.outcome in {"ran", "success"}:
            item.outcome = "failed"  # rolled back with the run
    if isinstance(error, KeyboardInterrupt):
        reason = RunError(
            code="STORE_CANCELLED",
            message="The run was interrupted; it rolled back and nothing was written.",
        )
    elif isinstance(error, _StepFailed):
        reason = RunError(code=error.code, message=error.message)
    else:
        reason = RunError(code=error.code, message=_store_message(error))
    if current is not None:
        current.finished_at = run_metadata._now()
        current.outcome = "failed"
        current.error = reason
        if isinstance(error, _StepFailed) and error.problems:
            return list(error.problems)
        return [
            Problem(
                reason.code,
                current.model.sql_file,
                None,
                f"{current.model.name}: {reason.message}"
                if isinstance(error, StoreError)
                else reason.message,
            )
        ]
    # Not a model's fault (the transaction could not start or commit, or the records could not
    # be written): every model failed for the same reason.
    for item in outcomes:
        item.outcome = "failed"
        item.error = reason
    return [Problem(reason.code, ".", None, reason.message)]
