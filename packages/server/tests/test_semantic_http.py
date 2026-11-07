"""P2-27a (#363): semantic `query` and `catalog` over HTTP for the project the server opened,
behind the serve session's separate credentials, with JSON and Arrow equal, and next pages on
their cursor's snapshot."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import jsonschema
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from orvex.access.local import LocalRequest
from orvex.semantic.canonical import canonicalize
from orvex.server import commands, semantic_http
from orvex.server.app import LocalSemantic, create_app
from orvex.server.cli import served_project
from orvex.server.identity import load_identity
from orvex.server.local_access import LocalAccess, new_access, serving
from orvex.server.project import load_project
from orvex.server.versions import Versions
from orvex.store.json_encoding import arrow_to_json

IDENTITY = load_identity()
VENDORS = (IDENTITY.ossie_vendor_name, *IDENTITY.previous_vendor_names)
SLUG = IDENTITY.slug
PORT = 7878
BASE = f"http://127.0.0.1:{PORT}"
VERSIONS = Versions("0.0.0", (), ())
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
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


@pytest.fixture
def served(tmp_path: Path) -> tuple[LocalSemantic, TestClient]:
    project(tmp_path)
    run(tmp_path)
    semantic = local(tmp_path)
    return semantic, client(semantic)


# --- credentials (Owner precision 1 and its corrections) -----------------------------------------


def test_the_bootstrap_code_works_once_and_a_wrong_guess_spends_nothing() -> None:
    access = new_access(SLUG, PORT)
    code = access.code
    assert access.exchange(code + "x") is None
    assert access.exchange("") is None
    assert access.exchange(None) is None
    session = access.exchange(code)
    assert session is not None
    assert session not in {code, access.bearer}
    assert access.exchange(code) is None  # spent
    with pytest.raises(ValueError, match="already used"):
        _ = access.code


def test_the_bootstrap_code_expires() -> None:
    now = [100.0]
    access = LocalAccess("c", clock=lambda: now[0], seconds=60)
    code = access.code
    now[0] = 161.0
    assert access.exchange(code) is None


def test_credentials_are_never_interchangeable() -> None:
    access = new_access(SLUG, PORT)
    code = access.code
    session = access.exchange(code)
    assert session is not None
    cookie = access.cookie
    assert access.accepts({}, {cookie: session})
    assert access.accepts({"authorization": f"Bearer {access.bearer}"}, {})
    for headers, cookies in (
        ({}, {cookie: code}),  # the bootstrap code is no session
        ({}, {cookie: access.bearer}),  # the scripts' token is no cookie
        ({"authorization": f"Bearer {session}"}, {}),  # a session is no bearer token
        ({"authorization": f"Bearer {code}"}, {}),
        ({"authorization": f"Basic {access.bearer}"}, {}),
        ({"authorization": "Bearer "}, {cookie: session}),  # the header alone decides
        ({}, {cookie: session + "x"}),
        ({}, {"other": session}),
    ):
        assert not access.accepts(headers, cookies), (headers, cookies)


def test_every_credential_is_revoked_and_its_files_removed_when_the_server_stops(
    tmp_path: Path,
) -> None:
    access = new_access(SLUG, PORT)
    with serving(tmp_path / "serve", PORT, access, f"{BASE}/") as files:
        for path in (files.token, files.launcher):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert files.token.read_text() == access.bearer
        page = files.launcher.read_text()
        assert f"{BASE}/?code={access.code}" in page
        assert 'content="no-referrer"' in page
        session = access.exchange(access.code)
        assert session is not None
        assert access.accepts({}, {access.cookie: session})
    assert not files.token.exists()
    assert not files.launcher.exists()
    assert not access.accepts({}, {access.cookie: session})
    assert not access.accepts({"authorization": f"Bearer {access.bearer}"}, {})


def test_no_credential_shows_in_a_representation() -> None:
    access = new_access(SLUG, PORT)
    for secret in (access.code, access.bearer):
        assert secret not in repr(access)
    settings = semantic_http.session_settings()
    assert "secret" not in repr(settings).lower().replace("cursors=", "")


# --- the session gate ---------------------------------------------------------------------------


class Untouched:
    """A source that fails the test if a refused request reaches it."""

    def session(self) -> Any:  # noqa: ANN401
        raise AssertionError("a refused request reached the project")

    def model(self, lake: Any, snapshot: int) -> Any:  # noqa: ANN401
        raise AssertionError("a refused request reached the project")


def gated() -> tuple[LocalSemantic, TestClient]:
    semantic = LocalSemantic(
        "project-1",
        new_access(SLUG, PORT),
        semantic_http.Semantic(
            Untouched(), semantic_http.local_context, semantic_http.session_settings()
        ),
    )
    return semantic, client(semantic)


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Bearer wrong"}, 401, "UNAUTHENTICATED"),
        ({"Host": "attacker.example:7878"}, 400, "HOST_NOT_ALLOWED"),  # DNS rebinding
        ({"Host": "127.0.0.1:9999"}, 400, "HOST_NOT_ALLOWED"),
        ({"Origin": "http://attacker.example"}, 403, "ORIGIN_NOT_ALLOWED"),
        ({"Origin": "null"}, 403, "ORIGIN_NOT_ALLOWED"),
        ({"Origin": "http://127.0.0.1:9999"}, 403, "ORIGIN_NOT_ALLOWED"),  # same site, other port
        ({"Sec-Fetch-Site": "cross-site"}, 403, "ORIGIN_NOT_ALLOWED"),
        ({"Sec-Fetch-Site": "same-site"}, 403, "ORIGIN_NOT_ALLOWED"),
    ],
)
def test_foreign_or_unauthenticated_requests_are_refused_before_any_project_work(
    headers: dict[str, str], status: int, code: str
) -> None:
    semantic, http = gated()
    valid = {} if code == "UNAUTHENTICATED" else bearer(semantic)
    for response in (
        http.post("/api/v1/semantic/query", json=aggregate(), headers={**valid, **headers}),
        http.get("/api/v1/semantic/catalog", headers={**valid, **headers}),
    ):
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("media", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_a_body_that_is_not_json_is_refused_so_no_simple_cross_site_request_gets_in(
    media: str,
) -> None:
    semantic, http = gated()
    response = http.post(
        "/api/v1/semantic/query",
        content=json.dumps(aggregate()),
        headers={**bearer(semantic), "Content-Type": media},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_the_code_is_exchanged_once_for_a_cookie_and_leaves_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    semantic, http = gated()
    code = semantic.access.code
    with caplog.at_level(logging.DEBUG):
        response = http.get(f"/dashboards?view=2&code={code}", follow_redirects=False)
    # Every log line the server writes; the test client's own (httpx) is not the server's.
    logged = "\n".join(
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.name.startswith("orvex")
    )
    assert logged
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboards?view=2"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{semantic.access.cookie}=")
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute in cookie
    for secret in (code, semantic.access.bearer):
        assert secret not in cookie
        assert secret not in logged
    again = http.get(f"/?code={code}", follow_redirects=False)
    assert again.status_code == 401
    assert http.post(f"/?code={code}").status_code == 401


def test_health_and_version_stay_open_and_hold_no_project_data() -> None:
    _, http = gated()
    assert http.get("/api/v1/health").status_code == 200
    assert http.get("/api/v1/version").status_code == 200


def test_no_semantic_route_exists_without_local_mode() -> None:
    http = TestClient(create_app(VERSIONS, None, PORT), base_url=BASE)
    assert http.post("/api/v1/semantic/query", json=aggregate()).status_code == 405 or (
        http.post("/api/v1/semantic/query", json=aggregate()).json()["error"]["code"]
        in {"NOT_FOUND", "METHOD_NOT_ALLOWED"}
    )
    assert http.get("/api/v1/semantic/catalog").json()["error"]["code"] == "NOT_FOUND"


# --- query and catalog --------------------------------------------------------------------------


def test_a_question_answers_with_its_result_and_receipt_view(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    response = ask(http, semantic, aggregate())
    assert response.status_code == 200, response.text
    document = response.json()
    assert list(schema("semantic-result").iter_errors(document["result"])) == []
    assert list(schema("receipt").iter_errors(document["receipt"])) == []
    assert document["receipt"]["view"] == "builder"
    rows = document["result"]["rows"]
    assert sorted(rows, key=str) == sorted(
        [["commercial", "-0.9999"], ["residential", "12.3456"]], key=str
    )


def test_the_catalog_validates_and_a_session_cookie_also_opens_it(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    session = semantic.access.exchange(semantic.access.code)
    assert session is not None
    http.cookies.set(semantic.access.cookie, session)
    response = http.get("/api/v1/semantic/catalog")
    assert response.status_code == 200, response.text
    assert list(schema("catalog").iter_errors(response.json())) == []


def test_policies_apply_over_http_and_hidden_members_are_never_listed(tmp_path: Path) -> None:
    project(tmp_path, POLICIES)
    run(tmp_path)
    semantic = local(tmp_path)
    http = client(semantic)
    catalog = json.dumps(http.get("/api/v1/semantic/catalog", headers=bearer(semantic)).json())
    assert "consumer_id" not in catalog
    hidden = ask(
        http,
        semantic,
        {**aggregate(), "group_by": [{"member": "readings.consumer_id"}]},
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "MEMBER_NOT_FOUND"
    masked = ask(
        http,
        semantic,
        {
            **detail(10),
            "fields": [{"member": "readings.reading_id"}, {"member": "readings.tariff"}],
        },
    )
    assert masked.status_code == 200, masked.text
    assert [row[1] for row in masked.json()["result"]["rows"]] == ["***"] * 4
    assert "consumer_id" not in json.dumps(masked.json()["receipt"])


def test_engine_errors_carry_their_semantic_message_and_status(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    unknown = ask(http, semantic, {**aggregate(), "metrics": [{"metric": "nothing"}]})
    assert unknown.status_code == 404
    error = unknown.json()["error"]
    assert error["code"] == "MEMBER_NOT_FOUND" == error["semantic"]["code"]
    assert error["semantic"]["audience"] == "builder"
    invalid = http.post(
        "/api/v1/semantic/query",
        content=b"{not json",
        headers={**bearer(semantic), "Content-Type": "application/json"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "QUERY_INVALID"


def test_a_project_without_a_model_or_a_lake_says_so(tmp_path: Path) -> None:
    write(tmp_path, {"project.yaml": PROJECT})
    semantic = local(tmp_path)
    http = client(semantic)
    response = http.get("/api/v1/semantic/catalog", headers=bearer(semantic))
    assert (response.status_code, response.json()["error"]["code"]) == (404, "LAKE_NOT_FOUND")
    assert not (tmp_path / f".{SLUG}").exists()  # asking created nothing


def test_a_model_with_problems_publishes_nothing(
    served: tuple[LocalSemantic, TestClient], tmp_path: Path
) -> None:
    semantic, http = served
    (tmp_path / "semantic" / "readings.ossie.json").write_text("{", "utf-8")
    response = ask(http, semantic, aggregate())
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "SEMANTIC_MODEL_INVALID",
    )


def test_a_run_holding_the_lake_makes_a_question_busy(
    served: tuple[LocalSemantic, TestClient], tmp_path: Path
) -> None:
    semantic, http = served
    with commands.project_lock(tmp_path / f".{SLUG}"):
        response = ask(http, semantic, aggregate())
    assert (response.status_code, response.json()["error"]["code"]) == (503, "PROJECT_BUSY")
    assert response.headers["retry-after"] == "5"


# --- Arrow and JSON (Owner precision 3) ---------------------------------------------------------


def arrow_of(response: Any) -> pa.Table:  # noqa: ANN401
    assert response.headers["content-type"] == semantic_http.ARROW
    return pa.ipc.open_stream(response.content).read_all()


def assert_equivalent(as_json: dict[str, Any], table: pa.Table) -> None:
    """The Arrow answer holds the JSON answer: its envelope and receipt byte for byte, and the
    same cells, with a state or a mask told from a value and from `NULL`."""
    metadata = table.schema.metadata
    result = dict(as_json["result"])
    rows = result.pop("rows")
    assert metadata[semantic_http.ENVELOPE_KEY] == canonicalize(result)
    assert metadata[semantic_http.RECEIPT_KEY] == canonicalize(as_json["receipt"])
    assert json.loads(metadata[b"semantic.cell_states"]) == result.get("cell_states", [])
    text = "".join(
        arrow_to_json(pa.RecordBatchReader.from_batches(table.schema, table.to_batches()))
    )
    values = json.loads(text)["rows"]
    states = {(s["row"], s["column"]) for s in result.get("cell_states", [])}
    assert len(values) == len(rows)
    for r, (value_row, json_row) in enumerate(zip(values, rows, strict=True)):
        for c, (value, cell) in enumerate(zip(value_row, json_row, strict=True)):
            if (r, c) in states:
                assert value is None
                assert isinstance(cell, dict)
                assert "state" in cell
            else:
                assert value == cell, (r, c, value, cell)
    for column, field in zip(result["columns"], table.schema, strict=True):
        assert field.name == column["name"]
        if column.get("encoding", "").startswith("DECIMAL"):
            assert pa.types.is_decimal(field.type)
            assert f"DECIMAL({field.type.precision},{field.type.scale})" == column["encoding"]


def both(semantic: LocalSemantic, query: dict[str, Any]) -> tuple[dict[str, Any], pa.Table]:
    """One answer in both forms, as the endpoint writes them."""
    asked = semantic_http.ask(
        semantic.semantic, json.dumps(query).encode(), LocalRequest(semantic.project, True)
    )
    sink = pa.BufferOutputStream()
    table = asked.arrow_table()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return asked.json_document(), pa.ipc.open_stream(sink.getvalue()).read_all()


def test_arrow_and_json_are_the_same_answer(served: tuple[LocalSemantic, TestClient]) -> None:
    semantic, http = served
    for query in (aggregate(), detail(10), detail(2)):
        assert_equivalent(*both(semantic, query))
        table = arrow_of(ask(http, semantic, query, Accept=semantic_http.ARROW))
        assert {semantic_http.ENVELOPE_KEY, semantic_http.RECEIPT_KEY} <= set(table.schema.metadata)
    nulls = ask(http, semantic, detail(10)).json()["result"]["rows"]
    assert [2, None] in nulls  # a real NULL stays NULL, never zero


def test_arrow_keeps_masks(tmp_path: Path) -> None:
    project(tmp_path, POLICIES)
    run(tmp_path)
    semantic = local(tmp_path)
    query = {
        **detail(10),
        "fields": [{"member": "readings.reading_id"}, {"member": "readings.tariff"}],
    }
    as_json, table = both(semantic, query)
    assert table.column("tariff").to_pylist() == ["***"] * 4
    assert_equivalent(as_json, table)


def test_accept_with_q_zero_does_not_ask_for_arrow(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    response = ask(
        http, semantic, aggregate(), Accept=f"{semantic_http.ARROW};q=0, application/json"
    )
    assert response.headers["content-type"].startswith("application/json")


# --- next pages on the cursor's snapshot (Owner precision 2) -------------------------------------


def test_a_next_page_is_compiled_and_read_on_its_cursor_s_snapshot(
    served: tuple[LocalSemantic, TestClient], tmp_path: Path
) -> None:
    semantic, http = served
    first = ask(http, semantic, detail(2)).json()["result"]
    after = first["page"]["next"]
    # A later run adds a row and changes the column's type: the next page must not see either.
    readings(
        tmp_path,
        FIRST.replace("DECIMAL(18,4)", "DECIMAL(20,6)")
        + ", (5, 'C4', 'residential', 9.0::DECIMAL(20,6))",
    )
    run(tmp_path)
    second = ask(http, semantic, detail(2, after))
    assert second.status_code == 200, second.text
    page = second.json()["result"]
    assert [row[0] for row in page["rows"]] == [3, 4]
    assert page["columns"][1]["encoding"] == first["columns"][1]["encoding"] == "DECIMAL(18,4)"
    assert "next" not in page["page"]  # row 5 belongs to the later snapshot only
    fresh = ask(http, semantic, detail(10)).json()["result"]
    assert [row[0] for row in fresh["rows"]] == [1, 2, 3, 4, 5]


def test_a_cursor_whose_snapshot_is_gone_is_expired_and_forged_ones_are_invalid(
    served: tuple[LocalSemantic, TestClient], tmp_path: Path
) -> None:
    semantic, http = served
    after = ask(http, semantic, detail(2)).json()["result"]["page"]["next"]
    tampered = after[:-2] + ("AA" if not after.endswith("AA") else "BB")
    other = semantic_http.session_settings().cursors  # another session's key
    for cursor in (tampered, "x" * 40):
        response = ask(http, semantic, detail(2, cursor))
        assert response.status_code == 400
        assert response.json()["error"]["semantic"]["details"] == {"reason": "invalid"}
    assert other.data_version(after) is None
    replayed = ask(http, semantic, detail(3, after))  # another page size: another binding
    assert replayed.json()["error"]["semantic"]["details"] == {"reason": "invalid"}
    run(tmp_path)  # a later snapshot; then maintenance expires every earlier one
    home = tmp_path / f".{SLUG}"
    with commands.project_lock(home):
        lake = commands.open_existing(home, commands.lake_location(load_project(tmp_path), home))
        try:
            later = datetime.now(UTC) + timedelta(days=1)
            lake.maintain(later, later)
        finally:
            lake.close()
    expired = ask(http, semantic, detail(2, after))
    assert expired.status_code == 400
    assert expired.json()["error"]["semantic"]["details"] == {"reason": "expired"}


def test_violated_evidence_refuses_with_409_before_any_read(tmp_path: Path) -> None:
    duplicated = FIRST + ", (1, 'C9', 'residential', 1.0::DECIMAL(18,4))"
    project(tmp_path, rows=duplicated, evidence=("unique",))
    run(tmp_path)
    semantic = local(tmp_path)
    response = ask(client(semantic), semantic, detail(10))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EVIDENCE_VIOLATED"


def test_verified_evidence_is_listed_in_the_receipt(tmp_path: Path) -> None:
    project(tmp_path, evidence=("unique",))
    run(tmp_path)
    semantic = local(tmp_path)
    receipt = ask(client(semantic), semantic, detail(10)).json()["receipt"]
    assert [e["status"] for e in receipt["evidence"]] == ["verified"]


def test_no_http_path_reaches_the_cli_s_builder_grant() -> None:
    """The CLI's local builder grant (#358) stays the CLI's: the HTTP code calls none of the
    commands that use it, and its caller comes from `access` alone."""
    import inspect

    from orvex.server import app

    for module in (app, semantic_http):
        source = inspect.getsource(module)
        for name in ("commands.evidence", "_check_semantic", "_evidence_after_run", "validate("):
            assert name not in source, (module.__name__, name)
    assert "LocalContextProvider" in inspect.getsource(semantic_http)


def test_the_exchange_never_redirects_to_another_host() -> None:
    semantic, http = gated()
    response = http.get(f"//evil.example/?code={semantic.access.code}", follow_redirects=False)
    if response.status_code == 303:
        assert response.headers["location"].startswith("/")
        assert not response.headers["location"].startswith("//")


def test_a_store_failure_is_a_500_with_a_request_id_only() -> None:
    from orvex.store.errors import StoreError

    class Failing:
        def session(self) -> Any:  # noqa: ANN401
            raise StoreError("STORE_IO", "disk /secret/path is gone", {"path": "/secret/path"})

        def model(self, lake: Any, snapshot: int) -> Any:  # noqa: ANN401
            raise AssertionError

    semantic = LocalSemantic(
        "project-1",
        new_access(SLUG, PORT),
        semantic_http.Semantic(
            Failing(), semantic_http.local_context, semantic_http.session_settings()
        ),
    )
    http = client(semantic)
    for response in (
        ask(http, semantic, aggregate()),
        http.get("/api/v1/semantic/catalog", headers=bearer(semantic)),
    ):
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "INTERNAL_ERROR"
        assert error["details"] == []
        assert "secret" not in response.text
        assert error["request_id"] == response.headers["x-request-id"]


def test_cell_states_and_the_others_row_are_the_same_in_arrow_and_json() -> None:
    """Owner precision 3 on a result with an "Others" row, a state in a column that also holds
    a real `NULL`, and a state elsewhere."""
    from orvex.semantic.result.building import CellState

    from conformance.runners.corpus_http import equivalent
    from tests.support.semantic_results import aggregate as built
    from tests.support.semantic_results import decimal, spec, table, text

    data = table(
        {
            "region": pa.array(["north", None, None]),
            "revenue": pa.array([1250, None, 310], pa.decimal128(18, 2)),
            "growth": pa.array([0.5, None, None], pa.float64()),
        }
    )
    columns = [
        text("region", role="dimension"),
        decimal("revenue", 18, 2, role="metric", member={"name": "revenue"}),
        spec("growth", "DOUBLE", {"family": "Float"}, role="metric", member={"name": "growth"}),
    ]
    states = [CellState(2, 0, "others"), CellState(2, 2, "undefined_at_total")]
    result = built(data, columns, states=states, others_row=2)
    receipt = {"kind": "receipt"}
    document = {"result": json.loads(result.wire(arrow_to_json)), "receipt": receipt}
    sink = pa.BufferOutputStream()
    answer = semantic_http.arrow_answer(result, receipt)
    with pa.ipc.new_stream(sink, answer.schema) as writer:
        writer.write_table(answer)
    arrived = pa.ipc.open_stream(sink.getvalue()).read_all()
    equivalent(document, arrived)
    assert_equivalent(document, arrived)
    rows = document["result"]["rows"]
    assert rows[1] == [None, None, None]  # real NULLs, no state
    assert rows[2][0] == {"state": "others"}
    assert rows[2][2] == {"state": "undefined_at_total"}
    assert arrived.column("region").to_pylist()[2] is None
    assert document["result"]["others_row"] == 2
    assert json.loads(arrived.schema.metadata[semantic_http.ENVELOPE_KEY])["others_row"] == 2
    assert str(arrived.schema.field("revenue").type) == "decimal128(18, 2)"
