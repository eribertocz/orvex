import asyncio
import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from orvex.server.app import create_app
from orvex.server.versions import Versions
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "http-api" / "v1" / "openapi.yaml"
DOCUMENT = yaml.safe_load(CONTRACT.read_text())
REGISTRY: Registry[bool | Mapping[str, Any]] = Registry().with_resource(
    "urn:http-api", Resource.from_contents(DOCUMENT, default_specification=DRAFT202012)
)
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))
PORT = 7878
BASE = f"http://127.0.0.1:{PORT}"  # how the browser addresses the local server


def conforms(body: object, schema: str) -> None:
    reference = {"$ref": f"urn:http-api#/components/schemas/{schema}"}
    Draft202012Validator(reference, registry=REGISTRY).validate(body)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    build = tmp_path / "build"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text("<html>app</html>")
    (build / "_app" / "main.js").write_text("console.log(1)")
    return TestClient(
        create_app(VERSIONS, build, PORT), base_url=BASE, raise_server_exceptions=False
    )


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
    app = create_app(VERSIONS, None, PORT)

    @app.get("/api/test/needs-number")
    async def needs_number(value: int) -> dict[str, int]:
        return {"value": value}

    response = TestClient(app, base_url=BASE).get("/api/test/needs-number", params={"value": "x"})
    assert response.status_code == 422
    conforms(response.json(), "ErrorEnvelope")
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"]


def test_unexpected_failure_is_an_internal_error_envelope() -> None:
    app = create_app(VERSIONS, None, PORT)

    @app.get("/api/test/fails")
    async def fails() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, base_url=BASE, raise_server_exceptions=False).get("/api/test/fails")
    assert response.status_code == 500
    conforms(response.json(), "ErrorEnvelope")
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in response.text


def test_every_code_the_server_sends_is_registered() -> None:
    sent = {
        "NOT_FOUND",
        "METHOD_NOT_ALLOWED",
        "HOST_NOT_ALLOWED",
        "ORIGIN_NOT_ALLOWED",
        "VALIDATION_FAILED",
        "INTERNAL_ERROR",
    }
    assert sent <= set(DOCUMENT["components"]["x-error-codes"])


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [("POST", "/api/v1/health", "GET"), ("DELETE", "/api/v1/version", "GET")],
)
def test_a_method_the_route_does_not_accept_is_method_not_allowed(
    client: TestClient, method: str, path: str, allowed: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 405
    conforms(response.json(), "ErrorEnvelope")
    error = response.json()["error"]
    assert error["code"] == "METHOD_NOT_ALLOWED"
    assert {"name": "method", "value": method} in error["details"]
    assert {"name": "path", "value": path} in error["details"]
    assert response.headers["Allow"] == allowed
    assert error["request_id"] == response.headers["X-Request-Id"]


def test_head_on_a_get_route_is_method_not_allowed(client: TestClient) -> None:
    response = client.head("/api/v1/health")  # a HEAD answer has headers only
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    assert response.headers["X-Request-Id"]


def test_the_log_records_the_405(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="orvex.server.http")
    response = client.post("/api/v1/health")
    record = next(r for r in caplog.records if r.getMessage() == "request")
    assert record.__dict__["status"] == 405
    assert record.__dict__["method"] == "POST"
    assert record.__dict__["request_id"] == response.headers["X-Request-Id"]


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/api/v1/unknown", 404, "NOT_FOUND"),
        ("POST", "/api/v1/unknown", 404, "NOT_FOUND"),
        ("POST", "/dashboards/arrears", 404, "NOT_FOUND"),  # no route; only GET serves pages
        ("POST", "/", 404, "NOT_FOUND"),
        ("PUT", "/api/v1/health", 405, "METHOD_NOT_ALLOWED"),
    ],
)
def test_routing_errors_have_their_own_codes(
    client: TestClient, method: str, path: str, status: int, code: str
) -> None:
    # 404 and 405 are the statuses the framework raises while routing; none may be presented as
    # an unexpected failure.
    response = client.request(method, path)
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert response.json()["error"]["code"] in DOCUMENT["components"]["x-error-codes"]


def test_static_files_and_client_side_routes(client: TestClient) -> None:
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/_app/main.js").text == "console.log(1)"
    assert client.get("/dashboards/arrears").text == "<html>app</html>"


def test_paths_outside_the_build_are_never_served(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret")
    assert "secret" not in client.get("/../secret.txt").text
    assert "secret" not in client.get("/%2e%2e/secret.txt").text


def test_without_a_web_build_only_the_api_answers() -> None:
    client = TestClient(create_app(VERSIONS, None, PORT), base_url=BASE)
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/health").status_code == 200


def test_request_log_has_request_id_and_no_query_values(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="orvex.server.http")
    response = client.get("/api/v1/health", params={"token": "s3cr3t"})
    record = next(r for r in caplog.records if r.getMessage() == "request")
    assert record.__dict__["request_id"] == response.headers["X-Request-Id"]
    assert record.__dict__["path"] == "/api/v1/health"
    assert "s3cr3t" not in json.dumps(record.__dict__, default=str)


# --- local mode answers only itself (#45: DNS rebinding, other sites) -------------------------


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"LOCALHOST:{PORT}"])
def test_requests_addressed_to_the_local_server_are_answered(client: TestClient, host: str) -> None:
    assert client.get("/api/v1/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["rebind.example:7878", f"127.0.0.1:{PORT + 1}", "127.0.0.1", f"evil.127.0.0.1:{PORT}"]
)
@pytest.mark.parametrize("path", ["/api/v1/health", "/", "/api/v1/unknown", "/dashboards/x"])
def test_another_host_is_refused_before_anything_else(
    client: TestClient, host: str, path: str
) -> None:
    response = client.get(path, headers={"Host": host})
    assert response.status_code == 400
    conforms(response.json(), "ErrorEnvelope")
    error = response.json()["error"]
    assert error["code"] == "HOST_NOT_ALLOWED"
    assert error["details"] == [{"name": "host", "value": host}]
    assert error["request_id"] == response.headers["X-Request-Id"]
    assert "access-control-allow-origin" not in response.headers


def test_a_long_host_is_shown_shortened(client: TestClient) -> None:
    host = "a" * 500 + f".example:{PORT}"
    detail = client.get("/api/v1/health", headers={"Host": host}).json()["error"]["details"][0]
    assert detail == {"name": "host", "value": host[:200]}


@pytest.mark.parametrize("path", ["/api/v1/health", "/", "/api/v1/unknown", "/dashboards/x"])
def test_a_request_without_host_is_refused(tmp_path: Path, path: str) -> None:
    # HTTP/1.0 allows no Host; the test client always sends one, so the app is called directly.
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<html>app</html>")
    app = create_app(VERSIONS, build, PORT)
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.0",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 400
    headers = {name.decode().lower(): value.decode() for name, value in sent[0]["headers"]}
    body = json.loads(b"".join(message.get("body", b"") for message in sent[1:]))
    conforms(body, "ErrorEnvelope")
    assert body["error"]["code"] == "HOST_NOT_ALLOWED"
    assert body["error"]["details"] == [{"name": "host", "value": ""}]
    assert body["error"]["request_id"] == headers["x-request-id"]
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}", f"https://127.0.0.1:{PORT}"],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_another_origin_is_refused(client: TestClient, origin: str, method: str) -> None:
    response = client.request(method, "/api/v1/health", headers={"Origin": origin})
    assert response.status_code == 403
    conforms(response.json(), "ErrorEnvelope")
    error = response.json()["error"]
    assert error["code"] == "ORIGIN_NOT_ALLOWED"
    assert error["details"] == [{"name": "origin", "value": origin}]
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("origin", [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"])
def test_the_servers_own_origin_is_accepted(client: TestClient, origin: str) -> None:
    response = client.get("/api/v1/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("method", "header", "value", "status"),
    [
        ("GET", "Host", "rebind.example:" + "9" * 300, 400),
        ("POST", "Origin", "http://" + "9" * 300, 403),
    ],
)
def test_a_refusal_is_logged_with_its_status(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    method: str,
    header: str,
    value: str,
    status: int,
) -> None:
    caplog.set_level("INFO", logger="orvex.server.http")
    response = client.request(method, "/api/v1/health", headers={header: value})
    assert response.status_code == status
    record = next(r for r in caplog.records if r.getMessage() == "request")
    assert record.__dict__["status"] == status
    assert record.__dict__["request_id"] == response.headers["X-Request-Id"]
    assert "9" * 201 not in json.dumps(record.__dict__, default=str)
