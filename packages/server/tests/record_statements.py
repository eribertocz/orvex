"""The transform contract as a run reaches it, with `record_statements` replaced: for the tests
that make a run's records fail, or watch the run write them."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from orvex.contracts.store.v1 import StatementRequest
from orvex.server import run as run_command
from orvex.server.transform_calls import transform

type Replacement = Callable[..., tuple[StatementRequest, ...]]


class RecordStatements:
    """Every operation as served, but `record_statements`, which is `replacement`."""

    def __init__(self, replacement: Replacement) -> None:
        self._served = transform()
        self.record_statements = replacement

    def __getattr__(self, name: str) -> object:
        return getattr(self._served, name)


def replace_record_statements(patch: pytest.MonkeyPatch, replacement: Replacement) -> None:
    """The run reaches transform with `record_statements` replaced, until `patch` is undone."""
    replaced = RecordStatements(replacement)
    patch.setattr(run_command, "transform", lambda: replaced)


def failing_record_statements(*_: object) -> tuple[StatementRequest, ...]:
    """Run-record statements that fail: the run's transaction rolls back."""
    return (
        StatementRequest(kind="statement", sql="INSERT INTO nowhere VALUES (1)", parameters=[]),
    )
