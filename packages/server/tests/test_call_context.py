"""The call context of each server entry point (#486 §2 item 6): one real correlation id per
request or command, and a context that refuses missing or placeholder values."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.server.app import create_app
from orvex.server.call_context import (
    command_context,
    request_context,
    request_correlation_id,
)
from orvex.server.versions import Versions
from starlette.requests import Request

VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))
PORT = 7878


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "index.html").write_text("<html>app</html>")
    app = create_app(VERSIONS, tmp_path, PORT)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_a_well_formed_request_id_is_kept_end_to_end(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "trace-0042.a_b"})
    assert response.headers["X-Request-Id"] == "trace-0042.a_b"


@pytest.mark.parametrize(
    "given", [None, "", "short", "x" * 129, "bad id with spaces", "id\r\nX-Evil: 1", "ñandú-1234"]
)
def test_a_missing_or_malformed_request_id_is_replaced(given: str | None) -> None:
    headers = {} if given is None else {"X-Request-Id": given}
    found = request_correlation_id(headers)
    assert found != given
    assert len(found) == 32
    assert found != request_correlation_id(headers)


def test_each_request_gets_its_own_id(client: TestClient) -> None:
    first = client.get("/api/v1/health").headers["X-Request-Id"]
    second = client.get("/api/v1/health").headers["X-Request-Id"]
    assert first != second


def test_a_request_s_context_carries_its_request_id() -> None:
    request = Request({"type": "http", "headers": [], "state": {"request_id": "trace-0042"}})
    context = request_context(request, "user:ana", "billing", "a1b2c3d")
    assert context.correlation_id == "trace-0042"
    assert (context.actor, context.project_id, context.revision) == (
        "user:ana",
        "billing",
        "a1b2c3d",
    )


def test_each_command_gets_its_own_correlation_id() -> None:
    first = command_context("scheduler:instance-1", "billing", "a1b2c3d")
    second = command_context("scheduler:instance-1", "billing", "a1b2c3d")
    assert first.correlation_id != second.correlation_id


@pytest.mark.parametrize(
    ("actor", "project", "revision"),
    [("", "billing", "r1"), ("unknown", "billing", "r1"), ("user:ana", "n/a", "r1")],
)
def test_an_entry_point_cannot_build_a_placeholder_context(
    actor: str, project: str, revision: str
) -> None:
    with pytest.raises(ContractCallRefused) as raised:
        command_context(actor, project, revision)
    assert raised.value.code == "CONTRACT_CONTEXT_INVALID"
