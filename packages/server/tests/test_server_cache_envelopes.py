"""The result cache's calls carry the HTTP entry point's call context (#486 §2 item 6): the
request's own `X-Request-Id` (or the id the server gave it), the caller, the project and the
snapshot the question reads; never a placeholder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from orvex.contracts.binding import Bound
from orvex.contracts.envelope import CallEnvelope

from tests.support import semantic_http as harness


@pytest.fixture
def envelopes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, CallEnvelope]]:
    found: list[tuple[str, CallEnvelope]] = []
    original = Bound.call

    def recorded(self: Bound, operation: str, envelope: object, *more: object) -> object:
        if self.contract.name == "result-cache" and isinstance(envelope, CallEnvelope):
            found.append((operation, envelope))
        return original(self, operation, envelope, *more)

    monkeypatch.setattr(Bound, "call", recorded)
    return found


def ask(tmp_path: Path, **headers: str) -> Any:  # noqa: ANN401
    harness.project(tmp_path)
    harness.run(tmp_path)
    semantic = harness.local(tmp_path)
    return harness.ask(harness.client(semantic), semantic, harness.aggregate(), **headers)


def test_the_cache_calls_carry_the_request_s_id_caller_project_and_snapshot(
    tmp_path: Path, envelopes: list[tuple[str, CallEnvelope]]
) -> None:
    answer = ask(tmp_path, **{"X-Request-Id": "request-0123456789"})
    assert answer.status_code == 200, answer.text
    assert [operation for operation, _ in envelopes] == ["get", "put"]
    receipt = answer.json()["receipt"]
    for _, envelope in envelopes:
        assert envelope.correlation_id == "request-0123456789"
        assert envelope.actor == "local"
        assert envelope.project_id == "project-1"
        assert envelope.revision.startswith("snapshot-")
    (_, put) = envelopes[1]
    assert put.idempotency_key == "request-0123456789-put"
    assert receipt["cache"] == "miss"


def test_a_request_without_an_id_gets_the_one_the_server_answers_with(
    tmp_path: Path, envelopes: list[tuple[str, CallEnvelope]]
) -> None:
    answer = ask(tmp_path)
    assert answer.status_code == 200, answer.text
    given = answer.headers["X-Request-Id"]
    assert {envelope.correlation_id for _, envelope in envelopes} == {given}
