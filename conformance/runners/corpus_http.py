"""Running a corpus case through the HTTP API (P2-27a, #363): the same case, lake, model and
caller as `corpus_execution`, asked through `POST /api/v1/semantic/query` of the server's own
application, with the serve session's bearer token.

Every answer is asked twice, as JSON and as Arrow, and the two must be the same answer (Owner
precision 3): the same envelope (the receipt reference apart, since each answer has its own
receipt), and the same cells, a state or a mask told from a value and from `NULL`. A refusal
comes back as the engine's own `semantic-error` message and is compared like the library's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
from fastapi.testclient import TestClient
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.errors import SemanticError
from orvex.semantic.policies import PolicyModel
from orvex.server import semantic_http, semantic_model
from orvex.server.app import LocalSemantic, create_app
from orvex.server.local_access import new_access
from orvex.server.versions import Versions
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce

PORT = 7878
BASE = f"http://127.0.0.1:{PORT}"


class Mismatch(AssertionError):
    """The JSON and Arrow forms of one answer differ."""


class _CaseSource:
    """The case's own lake and model, for one question."""

    def __init__(self, lake: Lake, compiled: CompiledModel, policies: PolicyModel) -> None:
        self.lake = lake
        self.compiled = compiled
        self.policies = policies

    @contextmanager
    def session(self) -> Iterator[Lake]:
        yield self.lake

    def model(self, lake: Lake, snapshot: int) -> semantic_model.Loaded:
        return semantic_model.Loaded(
            semantic_model.SemanticModel(self.compiled, self.policies, snapshot, "semantic/case")
        )


def _cells(envelope: Mapping[str, Any], rows: list[list[Any]], table: pa.Table) -> None:
    text = "".join(
        arrow_to_json(pa.RecordBatchReader.from_batches(table.schema, table.to_batches()))
    )
    values = json.loads(text)["rows"]
    states = {(s["row"], s["column"]) for s in envelope.get("cell_states", [])}
    if len(values) != len(rows):
        raise Mismatch("row count")
    for r, (value_row, json_row) in enumerate(zip(values, rows, strict=True)):
        for c, (value, cell) in enumerate(zip(value_row, json_row, strict=True)):
            if (r, c) in states:
                if value is not None or not (isinstance(cell, dict) and "state" in cell):
                    raise Mismatch(f"state at {r, c}")
            elif value != cell:
                raise Mismatch(f"cell at {r, c}: {value!r} != {cell!r}")


def equivalent(document: Mapping[str, Any], table: pa.Table) -> None:
    """`Mismatch` unless the Arrow answer is the JSON answer, receipt reference apart."""
    result = dict(document["result"])
    rows = result.pop("rows")
    carried = json.loads(table.schema.metadata[semantic_http.ENVELOPE_KEY])
    if {k: v for k, v in carried.items() if k != "receipt"} != {
        k: v for k, v in result.items() if k != "receipt"
    }:
        raise Mismatch("envelope")
    if json.loads(table.schema.metadata[b"semantic.cell_states"]) != result.get("cell_states", []):
        raise Mismatch("cell states")
    if [f.name for f in table.schema] != [c["name"] for c in result["columns"]]:
        raise Mismatch("columns")
    _cells(result, rows, table)


def http(
    query: str,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
    lake: Lake,
) -> ce.Answered:
    """The HTTP API's answer, checked equal in JSON and Arrow; a refusal is raised as the
    engine's error, rebuilt from its `semantic-error` message."""
    access = new_access("corpus", PORT)
    local = LocalSemantic(
        "corpus",
        access,
        semantic_http.Semantic(
            _CaseSource(lake, compiled, policies),
            lambda request, policies, snapshot: context,
            semantic_http.session_settings(),
        ),
    )
    client = TestClient(
        create_app(Versions("0.0.0", (), ()), None, PORT, local),
        base_url=BASE,
        raise_server_exceptions=True,
    )
    headers = {"Authorization": f"Bearer {access.bearer}", "Content-Type": "application/json"}
    response = client.post("/api/v1/semantic/query", content=query.encode(), headers=headers)
    if response.status_code != 200:
        message = response.json()["error"].get("semantic")
        if message is None:
            raise AssertionError(f"HTTP {response.status_code}: {response.text}")
        raise SemanticError(message["code"], message["details"])
    document = response.json()
    arrow = client.post(
        "/api/v1/semantic/query",
        content=query.encode(),
        headers={**headers, "Accept": semantic_http.ARROW},
    )
    equivalent(document, pa.ipc.open_stream(arrow.content).read_all())
    result = document["result"]
    rows = [[None if v is None else str(v) for v in row] for row in result["rows"]]
    return ce.Answered(result, rows)


def run_case(
    case: str,
    folder: Path,
    model_path: Path,
    expected_code: str | None,
    schema: str | None = None,
) -> ce.CaseRun:
    """`corpus_execution.run_case` through the HTTP API, every refusal included."""
    try:
        return ce.run_case(
            case, folder, model_path, expected_code, schema, ask=http, check_first=False
        )
    except Mismatch as error:
        return ce.CaseRun("fail", f"JSON and Arrow differ: {error}")
