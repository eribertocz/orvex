"""P0-08: contract compatibility check, run before the server starts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from orvex.server import cli
from orvex.server.app import create_app
from orvex.server.cli import describe_versions, main
from orvex.server.identity import load_identity
from orvex.server.versions import (
    ContractVersionIncompatible,
    Declaration,
    check_compatibility,
    current_versions,
    installed_declarations,
    parse_declaration,
)

IDENTITY = load_identity()
PRODUCER = parse_declaration("server", 'produces = { "http-api" = "v1" }\nconsumes = {}\n')


def consumer(accepts: str) -> Declaration:
    return parse_declaration(
        "dashboards", f'produces = {{}}\nconsumes = {{ "http-api" = {accepts} }}\n'
    )


def test_installed_modules_are_compatible() -> None:
    check_compatibility(installed_declarations(IDENTITY))


def test_accepted_version_passes() -> None:
    check_compatibility((PRODUCER, consumer('["v1", "v2"]')))


def test_consumer_accepting_only_v2_is_rejected() -> None:
    with pytest.raises(ContractVersionIncompatible) as raised:
        check_compatibility((PRODUCER, consumer('["v2"]')))
    message = str(raised.value)
    assert message.startswith("CONTRACT_VERSION_INCOMPATIBLE: ")
    assert "http-api" in message
    assert "server offers v1" in message
    assert "dashboards accepts v2" in message


def test_consumed_contract_without_producer_is_rejected() -> None:
    with pytest.raises(ContractVersionIncompatible, match="no installed module produces it"):
        check_compatibility((consumer('["v1"]'),))


def test_two_producers_are_rejected() -> None:
    other = parse_declaration("mcp", 'produces = { "http-api" = "v1" }\n')
    with pytest.raises(ContractVersionIncompatible, match="more than one producer"):
        check_compatibility((PRODUCER, other))


@pytest.mark.parametrize(
    "text",
    [
        'produces = { "http-api" = "1" }',
        'consumes = { "http-api" = [] }',
        'consumes = { "x" = "v1" }',
    ],
)
def test_malformed_declarations_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="dashboards"):
        parse_declaration("dashboards", text)


def test_incompatible_start_fails_before_opening_a_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("must not be reached")

    monkeypatch.setattr(cli, "installed_declarations", lambda _: (PRODUCER, consumer('["v2"]')))
    monkeypatch.setattr(cli, "free_port", forbidden)
    monkeypatch.setattr(cli, "open_project", forbidden)
    monkeypatch.setattr(cli, "in_memory_project", forbidden)
    assert main(["--no-browser"]) == 3
    assert "CONTRACT_VERSION_INCOMPATIBLE" in capsys.readouterr().err


def test_cli_and_api_report_the_same_versions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    printed = capsys.readouterr().out.strip()
    versions = current_versions(IDENTITY)
    app = create_app(versions, None, 7878)
    body = TestClient(app, base_url="http://127.0.0.1:7878").get("/api/v1/version").json()
    from_api = describe_versions(
        IDENTITY,
        type(versions)(
            body["product"],
            tuple((item["name"], item["version"]) for item in body["modules"]),
            tuple((item["name"], item["version"]) for item in body["contracts"]),
        ),
    )
    assert printed == from_api
    assert ("http-api", "v1") in versions.contracts
