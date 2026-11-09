"""One model's run steps in the run's transaction: statements, landings, checks, watermarks
and counts, and the failure that stops the run."""

from __future__ import annotations

from collections.abc import Sequence

from orvex.contracts.run_record.v1 import CheckResult
from orvex.contracts.store.v1 import StatementRequest
from orvex.server.problems import Problem
from orvex.server.run_metadata import _Outcome
from orvex.store.lake import Transaction
from orvex.transform import incremental
from orvex.transform.incremental import Count, Land, Refuse, Step, Watermark
from orvex.transform.model_tests import ModelTest
from orvex.transform.reconciliation import Reconcile


class _StepFailed(Exception):
    """A model's run step refused to go on (a check failed, or the model cannot run)."""

    def __init__(self, code: str, message: str, problems: Sequence[Problem] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = tuple(problems)


def _check_tests(item: _Outcome) -> None:
    """After every test and reconciliation check of a model ran: the run stops if any failed
    (MODEL_TEST_FAILED first, then RECONCILIATION_FAILED); every result is recorded."""
    file = item.model.config_file or item.model.sql_file
    for code, results, unit in (
        ("MODEL_TEST_FAILED", item.tests, "rows"),
        ("RECONCILIATION_FAILED", item.reconciliation, "groups"),
    ):
        failed = [result for result in results if result.outcome == "failed"]
        if not failed:
            continue
        lines = [f"{result.name} failed on {result.failing_rows} {unit}" for result in failed]
        raise _StepFailed(
            code,
            f"{item.model.name}: " + "; ".join(lines) + ".",
            [Problem(code, file, None, line) for line in lines],
        )


def _run_step(transaction: Transaction, step: Step | ModelTest | Reconcile, item: _Outcome) -> None:
    if isinstance(step, Reconcile) and step.land is not None:
        # The database source's groups, landed from its worker, compared, then dropped (#372).
        landing = step.land
        transaction.land(landing.source, landing.sql, [], landing.schema, landing.table)
        failing = transaction.value(step.request) or "0"
        staged = f'"{landing.schema}"."{landing.table}"'
        for sql in (f"DROP TABLE {staged}", f'DROP SCHEMA IF EXISTS "{landing.schema}"'):
            transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
        item.reconciliation.append(
            CheckResult(
                name=step.name,
                outcome="passed" if failing == "0" else "failed",
                failing_rows=failing,
            )
        )
    elif isinstance(step, ModelTest | Reconcile):
        failing = transaction.value(step.request) or "0"
        results = item.tests if isinstance(step, ModelTest) else item.reconciliation
        results.append(
            CheckResult(
                name=step.name,
                outcome="passed" if failing == "0" else "failed",
                failing_rows=failing,
            )
        )
    elif isinstance(step, StatementRequest):
        transaction.execute(step)
    elif isinstance(step, Land):
        # A database source, read in its own worker and landed as one Arrow stream (#372).
        transaction.land(step.source, step.sql, list(step.parameters), step.schema, step.table)
    elif isinstance(step, Refuse):
        raise _StepFailed(step.code, step.message)
    elif isinstance(step, Watermark):
        # Stored with the rows, in this transaction: it moves only if they commit.
        value = transaction.value(step.request)
        for statement in incremental.watermark_statements(step, value):
            transaction.execute(statement)
        item.watermark = value
    elif isinstance(step, Count):
        value = transaction.value(StatementRequest(kind="statement", sql=step.sql, parameters=[]))
        item.rows_read = value or "0"
        item.rows_written = item.rows_read
    else:
        value = transaction.value(
            StatementRequest(kind="statement", sql=step.sql, parameters=list(step.parameters))
        )
        if value not in {None, "0"}:
            raise _StepFailed(step.code, step.failure(value))
