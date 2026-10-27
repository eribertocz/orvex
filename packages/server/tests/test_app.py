import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sextant.server.app import create_app
from sextant.server.versions import Versions

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "http-api" / "v1" / "openapi.yaml"
DOCUMENT = yaml.safe_load(CONTRACT.read_text())
REGISTRY: Registry[bool | Mapping[str, Any]] = Registry().with_resource(
    "urn:http-api", Resource.from_contents(DOCUMENT, default_specification=DRAFT202012)
)
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


def conforms(body: object, schema: str) -> None:
    reference = {"$ref": f"urn:http-api#/components/schemas/{schema}"}
    Draft202012Validator(reference, registry=REGISTRY).validate(body)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    build = tmp_path / "build"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text("<html>app</html>")
    (build / "_app" / "main.js").write_text("console.log(1)")
    return TestClient(create_app(VERSIONS, build), raise_server_exceptions=False)


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    conforms(response.json(), "Health")
    assert response.headers["X-Request-Id"]


def test_version(client: TestClient) -> None:
    body = client.get("/api/v1/version").json()
    conforms(body, "Version")
    assert body == {
        "product": "0.1.0",
        "modules": [{"name": "server", "version": "0.1.0"}],
        "contracts": [{"name": "http-api", "version": "v1"}],
    }


def test_unknown_api_path_is_a_not_found_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/unknown")
    assert response.status_code == 404
    conforms(response.json(), "ErrorEnvelope")
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]


def test_validation_error_is_an_envelope(tmp_path: Path) -> None:
    app = create_app(VERSIONS, None)

    @app.get("/api/test/needs-number")
    async def needs_number(value: int) -> dict[str, int]:
        return {"value": value}

    response = TestClient(app).get("/api/test/needs-number", params={"value": "x"})
    assert response.status_code == 422
    conforms(response.json(), "ErrorEnvelope")
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"]


def test_unexpected_failure_is_an_internal_error_envelope() -> None:
    app = create_app(VERSIONS, None)

    @app.get("/api/test/fails")
    async def fails() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/api/test/fails")
    assert response.status_code == 500
    conforms(response.json(), "ErrorEnvelope")
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in response.text


def test_every_code_the_server_sends_is_registered() -> None:
    sent = {"NOT_FOUND", "VALIDATION_FAILED", "INTERNAL_ERROR"}
    assert sent <= set(DOCUMENT["components"]["x-error-codes"])


def test_static_files_and_client_side_routes(client: TestClient) -> None:
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/_app/main.js").text == "console.log(1)"
    assert client.get("/dashboards/arrears").text == "<html>app</html>"


def test_paths_outside_the_build_are_never_served(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret")
    assert "secret" not in client.get("/../secret.txt").text
    assert "secret" not in client.get("/%2e%2e/secret.txt").text


def test_without_a_web_build_only_the_api_answers() -> None:
    client = TestClient(create_app(VERSIONS, None))
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/health").status_code == 200


def test_request_log_has_request_id_and_no_query_values(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sextant.server.http")
    response = client.get("/api/v1/health", params={"token": "s3cr3t"})
    record = next(r for r in caplog.records if r.getMessage() == "request")
    assert record.__dict__["request_id"] == response.headers["X-Request-Id"]
    assert record.__dict__["path"] == "/api/v1/health"
    assert "s3cr3t" not in json.dumps(record.__dict__, default=str)
