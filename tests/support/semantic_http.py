"""Helpers for the semantic HTTP routes' tests (P2-27a #363, P2-27b #364): a small project
with a lake, the server's local composition for it, and the questions asked of it."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import duckdb
import jsonschema
from fastapi.testclient import TestClient
from orvex.server import commands, semantic_http
from orvex.server.app import LocalSemantic, create_app
from orvex.server.cli import served_project
from orvex.server.identity import load_identity
from orvex.server.local_access import LocalAccess, new_access
from orvex.server.versions import Versions

IDENTITY = load_identity()
VENDORS = (IDENTITY.ossie_vendor_name, *IDENTITY.previous_vendor_names)
SLUG = IDENTITY.slug
PACKAGE = semantic_http.__name__.split(".", 1)[0]  # the server's own loggers
PORT = 7878
BASE = f"http://127.0.0.1:{PORT}"
VERSIONS = Versions("0.0.0", (), ())
CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = """\
version: 1
connections:
  archive:
    type: parquet
    path: data/readings.parquet
"""
MODELS = {
    "models/marts/readings.sql": "SELECT reading_id, consumer_id, tariff, kwh FROM archive.data",
}


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def readings(folder: Path, rows: str) -> None:
    data = folder / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        f"COPY (SELECT * FROM (VALUES {rows}) AS t(reading_id, consumer_id, tariff, kwh)) "
        f"TO '{data / 'readings.parquet'}'"
    )
    connection.close()


FIRST = (
    "(1, 'C1', 'residential', 12.3456::DECIMAL(18,4)), (2, 'C1', 'residential', NULL), "
    "(3, 'C2', 'commercial', -1.0000::DECIMAL(18,4)), "
    "(4, 'C3', 'commercial', 0.0001::DECIMAL(18,4))"
)


def extension(payload: dict[str, Any]) -> dict[str, Any]:
    return {"vendor_name": VENDORS[0], "data": json.dumps({"version": 1, **payload})}


def model(evidence: tuple[str, ...] = ()) -> str:
    extensions = [extension({"element": "dataset", "evidence": list(evidence)})] if evidence else []
    return json.dumps(
        {
            "version": "0.2.0.dev0",
            "name": "readings",
            "datasets": [
                {
                    "name": "readings",
                    "source": "marts.readings",
                    "primary_key": ["reading_id"],
                    "fields": [
                        {"name": c, "expression": {"sql": c}}
                        for c in ("reading_id", "consumer_id", "tariff", "kwh")
                    ],
                    "custom_extensions": extensions,
                }
            ],
            "metrics": [{"name": "total_kwh", "expression": {"sql": "SUM(readings.kwh)"}}],
            "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
        }
    )


POLICIES = """\
version: 1
column_policies:
  - member: readings.tariff
    default: masked
    mask: "***"
  - member: readings.consumer_id
    default: hidden
"""


def project(
    folder: Path, policies: str | None = None, rows: str = FIRST, evidence: tuple[str, ...] = ()
) -> Path:
    readings(folder, rows)
    files = {
        "project.yaml": PROJECT,
        "connections.yaml": CONNECTIONS,
        **MODELS,
        "semantic/readings.ossie.json": model(evidence),
    }
    if policies is not None:
        files["security.yaml"] = policies
    return write(folder, files)


def run(folder: Path) -> None:
    out = io.StringIO()
    assert commands.run(folder, IDENTITY, out) == 0, out.getvalue()


def local(folder: Path, access: LocalAccess | None = None) -> LocalSemantic:
    return LocalSemantic(
        "project-1",
        access or new_access(SLUG, PORT),
        semantic_http.Semantic(
            served_project(folder, IDENTITY),
            semantic_http.local_context,
            semantic_http.session_settings(),
        ),
    )


def client(semantic: LocalSemantic) -> TestClient:
    return TestClient(
        create_app(VERSIONS, None, PORT, semantic), base_url=BASE, raise_server_exceptions=False
    )


def bearer(semantic: LocalSemantic) -> dict[str, str]:
    return {"Authorization": f"Bearer {semantic.access.bearer}"}


def aggregate() -> dict[str, Any]:
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "group_by": [{"member": "readings.tariff"}],
        "metrics": [{"metric": "total_kwh"}],
    }


def detail(size: int = 2, after: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {"size": str(size)}
    if after is not None:
        page["after"] = after
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "readings.reading_id"}, {"member": "readings.kwh"}],
        "order": [{"by": "readings.reading_id", "direction": "asc"}],
        "page": page,
    }


def ask(http: TestClient, semantic: LocalSemantic, query: dict[str, Any], **more: Any) -> Any:  # noqa: ANN401
    return http.post("/api/v1/semantic/query", json=query, headers={**bearer(semantic), **more})


def schema(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / name / "v1" / "schema.json").read_text("utf-8"))
    )
