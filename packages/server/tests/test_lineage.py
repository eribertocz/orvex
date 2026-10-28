"""P1-18: every run writes OpenLineage events, and sends them when a collector is declared."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import jsonschema
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity
from referencing import Registry, Resource

REPOSITORY = Path(__file__).resolve().parents[3]
SPEC = REPOSITORY / "packages" / "transform" / "tests" / "openlineage"
SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test Lineage\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  archive:\n    type: parquet\n    path: data/r.parquet\n"
MODELS = {
    "models/staging/readings.sql": "SELECT reading_id, consumer_id, kwh FROM archive.data",
    "models/marts/by_consumer.sql": (
        "SELECT r.consumer_id, sum(r.kwh) AS kwh FROM staging.readings AS r GROUP BY ALL"
    ),
    "models/marts/copy.sql": "SELECT * FROM staging.readings",
}


def run_event_validator() -> jsonschema.Draft202012Validator:
    resources = []
    for path in [SPEC / "OpenLineage.json", *sorted((SPEC / "facets").glob("*.json"))]:
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append((document["$id"], Resource.from_contents(document)))
    registry: Registry[Any] = Registry().with_resources(resources)
    return jsonschema.Draft202012Validator(
        {"$ref": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"},
        registry=registry,
    )


def project(folder: Path, extra: dict[str, str] | None = None, settings: str = "") -> None:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        "COPY (SELECT * FROM (VALUES (1, 'C1', 1.5::DECIMAL(18,4)), (2, 'C2', 2.0::DECIMAL(18,4)))"
        f" AS t(reading_id, consumer_id, kwh)) TO '{folder / 'data' / 'r.parquet'}'"
    )
    files = {
        "project.yaml": PROJECT + settings,
        "connections.yaml": CONNECTIONS,
        **MODELS,
        **(extra or {}),
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def written(folder: Path) -> list[dict[str, Any]]:
    (path,) = (folder / f".{SLUG}" / "lineage").glob("*.ndjson")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_run_writes_valid_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    events = written(tmp_path)
    validator = run_event_validator()
    for event in events:
        validator.validate(event)
    # Run order (dependencies first, then by name): START then COMPLETE for each model.
    assert [(e["job"]["name"], e["eventType"]) for e in events] == [
        ("staging.readings", "START"),
        ("staging.readings", "COMPLETE"),
        ("marts.by_consumer", "START"),
        ("marts.by_consumer", "COMPLETE"),
        ("marts.copy", "START"),
        ("marts.copy", "COMPLETE"),
    ]
    by_job = {e["job"]["name"]: e for e in events if e["eventType"] == "COMPLETE"}
    readings = by_job["staging.readings"]
    assert readings["inputs"] == [
        {"namespace": "file", "name": str(tmp_path / "data" / "r.parquet")}
    ]
    assert readings["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 2
    lineage = by_job["marts.by_consumer"]["outputs"][0]["facets"]["columnLineage"]["fields"]
    assert lineage["kwh"]["inputFields"][0]["transformations"] == [
        {"type": "DIRECT", "subtype": "AGGREGATION"}
    ]
    assert "columnLineage" not in by_job["marts.copy"]["outputs"][0]["facets"]
    # One parent run for the whole run.
    parents = {e["run"]["facets"]["parent"]["run"]["runId"] for e in events}
    assert len(parents) == 1


def test_a_failed_run_ends_its_started_models_with_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(
        tmp_path,
        {
            "models/marts/copy.yaml": "version: 1\ntests:\n  - accepted_values: "
            "{column: consumer_id, values: [C1]}\n"
        },
    )
    code, _ = command(capsys, "run", str(tmp_path))
    assert code == 1
    events = written(tmp_path)
    # staging.readings and marts.by_consumer ran before marts.copy failed: rolled back too.
    finished = {e["job"]["name"]: e for e in events if e["eventType"] != "START"}
    assert {name: e["eventType"] for name, e in finished.items()} == {
        "staging.readings": "FAIL",
        "marts.by_consumer": "FAIL",
        "marts.copy": "FAIL",
    }
    assert "errorMessage" in finished["marts.copy"]["run"]["facets"]
    assert "errorMessage" not in finished["staging.readings"]["run"]["facets"]


class Collector(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        Collector.received.append((self.headers.get("Authorization", ""), body))
        self.send_response(201)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture
def collector() -> Iterator[str]:
    Collector.received = []
    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/v1/lineage"
    finally:
        server.shutdown()
        server.server_close()


def test_events_reach_the_collector_with_the_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    collector: str,
) -> None:
    monkeypatch.setenv("LINEAGE_TEST_KEY", "very-secret-key")
    project(
        tmp_path, settings=f"lineage:\n  url: {collector}\n  api_key: ${{env:LINEAGE_TEST_KEY}}\n"
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert len(Collector.received) == 6
    assert {auth for auth, _ in Collector.received} == {"Bearer very-secret-key"}
    assert [body for _, body in Collector.received] == written(tmp_path)
    folder = tmp_path / f".{SLUG}"
    assert all("very-secret-key" not in p.read_text() for p in folder.rglob("*.ndjson"))
    assert "very-secret-key" not in out


def test_a_collector_that_is_down_never_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, settings="lineage:\n  url: http://127.0.0.1:9/api/v1/lineage\n")
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "LINEAGE_NOT_SENT" in out
    assert len(written(tmp_path)) == 6


@pytest.mark.parametrize(
    "settings",
    [
        "lineage:\n  url: http://collector.example.com/api\n",
        "lineage:\n  url: https://collector.example.com/api\n  api_key: plain-text-key\n",
        "lineage:\n  url: https://user:plain-text-key@collector.example.com/api\n",
    ],
)
def test_unsafe_collector_settings_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], settings: str
) -> None:
    project(tmp_path, settings=settings)
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("PROJECT_SCHEMA_INVALID project.yaml")
    assert "plain-text-key" not in out


def test_models_that_never_started_emit_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(
        tmp_path,
        {
            "models/staging/readings.yaml": "version: 1\ntests:\n  - accepted_values: "
            "{column: consumer_id, values: [C1]}\n"
        },
    )
    code, _ = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert [(e["job"]["name"], e["eventType"]) for e in written(tmp_path)] == [
        ("staging.readings", "START"),
        ("staging.readings", "FAIL"),
    ]


class Redirect(BaseHTTPRequestHandler):
    target = ""
    hits: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.send_response(302)
        self.send_header("Location", Redirect.target)
        self.end_headers()

    def do_GET(self) -> None:
        Redirect.hits.append(self.headers.get("Authorization", ""))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def test_redirects_are_refused_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    servers = [HTTPServer(("127.0.0.1", 0), Redirect) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    Redirect.hits = []
    Redirect.target = f"http://127.0.0.1:{servers[1].server_address[1]}/elsewhere"
    try:
        monkeypatch.setenv("LINEAGE_TEST_KEY", "very-secret-key")
        url = f"http://127.0.0.1:{servers[0].server_address[1]}/api/v1/lineage"
        project(
            tmp_path, settings=f"lineage:\n  url: {url}\n  api_key: ${{env:LINEAGE_TEST_KEY}}\n"
        )
        code, out = command(capsys, "run", str(tmp_path))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert code == 0, out
    assert "LINEAGE_NOT_SENT" in out
    assert Redirect.hits == []  # the key never followed the redirect


def test_the_second_of_three_models_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(
        tmp_path,
        {
            "models/marts/by_consumer.yaml": "version: 1\ntests:\n  - accepted_values: "
            "{column: consumer_id, values: [C1]}\n"
        },
    )
    code, _ = command(capsys, "run", str(tmp_path))
    assert code == 1
    events = written(tmp_path)
    assert [(e["job"]["name"], e["eventType"]) for e in events] == [
        ("staging.readings", "START"),
        ("staging.readings", "FAIL"),
        ("marts.by_consumer", "START"),
        ("marts.by_consumer", "FAIL"),
    ]
    assert "errorMessage" not in events[1]["run"]["facets"]
    assert "errorMessage" in events[3]["run"]["facets"]


def test_a_malformed_collector_answer_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b"this is not HTTP\r\n\r\n")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/api"
        project(tmp_path, settings=f"lineage:\n  url: {url}\n")
        code, out = command(capsys, "run", str(tmp_path))
    finally:
        listener.close()
    assert code == 0, out
    assert "LINEAGE_NOT_SENT" in out


def test_a_local_collector_is_reached_without_the_proxy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    collector: str,
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    project(tmp_path, settings=f"lineage:\n  url: {collector}\n")
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "LINEAGE_NOT_SENT" not in out
    assert len(Collector.received) == 6


def test_column_lineage_follows_the_select_after_a_column_is_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir(parents=True)
    duckdb.connect().execute(
        "COPY (SELECT 1::BIGINT AS reading_id, 'C1' AS consumer_id, 1.5::DECIMAL(18,4) AS kwh, "
        "TIMESTAMPTZ '2024-01-01 10:00:00+00' AS read_at) "
        f"TO '{tmp_path / 'data' / 'r.parquet'}'"
    )
    sidecar = (
        "version: 1\nmaterialized: incremental\non_schema_change: append_new_columns\n"
        "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
    )
    files = {
        "project.yaml": PROJECT,
        "connections.yaml": CONNECTIONS,
        "models/s/m.sql": (
            "SELECT reading_id, kwh, read_at FROM archive.data WHERE read_at > $since"
        ),
        "models/s/m.yaml": sidecar,
    }
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert command(capsys, "run", str(tmp_path))[0] == 0
    (tmp_path / "models" / "s" / "m.sql").write_text(
        "SELECT reading_id, consumer_id AS tag, kwh, read_at FROM archive.data "
        "WHERE read_at >= $since",
        encoding="utf-8",
    )
    for old in (tmp_path / f".{SLUG}" / "lineage").glob("*.ndjson"):
        old.unlink()
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    (complete,) = [e for e in written(tmp_path) if e["eventType"] == "COMPLETE"]
    facets = complete["outputs"][0]["facets"]
    # The table keeps its order (tag added at the end); lineage follows the SELECT.
    assert [f["name"] for f in facets["schema"]["fields"]] == [
        "reading_id",
        "kwh",
        "read_at",
        "tag",
    ]
    fields = facets["columnLineage"]["fields"]
    assert fields["tag"]["inputFields"][0]["field"] == "consumer_id"
    assert fields["kwh"]["inputFields"][0]["field"] == "kwh"
