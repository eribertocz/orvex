"""#445 AC1, AC1b, AC2, AC3, AC8 over HTTP in server mode: every request passes authentication,
then origin/CSRF, then authorization, then the content check, in that order; a route without a
declared permission stops the app; only Builders and Authors ask free-form questions (a Viewer's
refusal is audited); each role's concurrency and run-time limits hold, with no limit on rows;
and server mode does not start without its audit log."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from orvex.access.authentication.query_limits import QueryLimits, RoleLimit
from orvex.access.errors import AccessError
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import answer
from orvex.server import semantic_delivery, semantic_http
from orvex.server.route_permissions import PERMISSIONS, checked_permissions
from orvex.server.server_app import ServedProject, create_server_app

from tests.support.access_directory import audit_events
from tests.support.semantic_http import IDENTITY, VERSIONS, aggregate, detail

from .test_server_semantic import ORIGIN, SAME, Server, ask

AGGREGATE = aggregate()
FOREIGN = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
TEXT = {"Content-Type": "text/plain"}
QUESTIONS = [
    ("POST", "/api/v1/semantic/query"),
    ("POST", "/api/v1/semantic/explain"),
    ("GET", "/api/v1/semantic/catalog"),
    ("GET", "/api/v1/semantic/jobs/0123456789abcdef0123456789abcdef"),
    ("DELETE", "/api/v1/semantic/jobs/0123456789abcdef0123456789abcdef"),
]


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def code(answer: Any) -> tuple[int, str]:  # noqa: ANN401
    return answer.status_code, answer.json()["error"]["code"]


def send(http: Any, method: str, path: str, headers: dict[str, str]) -> Any:  # noqa: ANN401
    body = b"{}" if method == "POST" else None
    return http.request(method, path, content=body, headers=headers)


# --- AC1: the order of the request rules ---------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), QUESTIONS)
def test_without_a_session_every_route_is_unauthenticated_first(
    server: Server, method: str, path: str
) -> None:
    from fastapi.testclient import TestClient

    http = TestClient(server.app, base_url=ORIGIN)
    assert code(send(http, method, path, {**FOREIGN, **TEXT})) == (401, "SESSION_EXPIRED")


@pytest.mark.parametrize(("method", "path"), [q for q in QUESTIONS if q[0] != "GET"])
def test_a_viewers_foreign_state_change_is_refused_for_its_origin_first(
    server: Server, method: str, path: str
) -> None:
    server.viewer("vera")
    answer = send(server.client("vera"), method, path, {**FOREIGN, **TEXT})
    assert code(answer) == (403, "ORIGIN_REFUSED")
    assert "content-type" not in answer.text  # the later steps tell nothing


@pytest.mark.parametrize(("method", "path"), QUESTIONS)
def test_a_viewer_is_refused_by_role_before_the_content_is_read(
    server: Server, method: str, path: str
) -> None:
    vera = server.viewer("vera")
    http = server.client("vera")
    opened: list[object] = []
    source = server.semantic.source
    session = source.session

    def spied() -> Any:  # noqa: ANN401
        opened.append(1)
        return session()

    source.session = spied  # type: ignore[method-assign]
    answer = send(http, method, path, {**SAME, **TEXT})
    assert code(answer) == (403, "ACCESS_FORBIDDEN")
    assert opened == []  # refused before any lake work
    refusals = [
        e for e in audit_events(server.store)
        if e["kind"] == "access.refused" and e["actor"] == vera
    ]  # fmt: skip
    assert refusals[-1]["details"] == {"operation": "route", "code": "ACCESS_FORBIDDEN"}
    assert refusals[-1]["project"] == "project-1"


@pytest.mark.parametrize(("method", "path"), [q for q in QUESTIONS if q[0] == "POST"])
def test_an_authors_wrong_content_is_refused_last(server: Server, method: str, path: str) -> None:
    server.author("ana")
    answer = send(server.client("ana"), method, path, {**SAME, **TEXT})
    assert code(answer) == (403, "ORIGIN_REFUSED")
    assert "content-type" in answer.text


def test_a_route_without_a_declared_permission_stops_the_app() -> None:
    app = FastAPI()

    @app.get("/api/v1/undeclared")
    async def undeclared() -> None:
        return None

    with pytest.raises(ValueError, match="/api/v1/undeclared"):
        checked_permissions(app)


def test_every_route_of_the_server_app_has_its_permission(server: Server) -> None:
    checked_permissions(server.app)  # it built: this is what the app checked itself
    assert PERMISSIONS["/api/v1/semantic/query"] == "explorer"
    assert PERMISSIONS["/api/v1/semantic/jobs/{job}"] == "explorer"


# --- AC2: who may ask ----------------------------------------------------------------------------


def test_a_viewer_still_has_their_session_routes(server: Server) -> None:
    server.viewer("vera")
    assert server.client("vera").get("/api/v1/auth/session").status_code == 200


def test_a_builder_and_an_author_ask(server: Server) -> None:
    server.author("ana")
    for name in ("builder", "ana"):
        assert ask(server.client(name), detail(1)).status_code == 200


def test_a_role_lowered_to_viewer_asks_no_more(server: Server) -> None:
    ana = server.author("ana")
    assert ask(server.client("ana"), detail(1)).status_code == 200
    server.directory.change_role(server.builder, "project-1", ana, "viewer")
    assert code(ask(server.client("ana"), detail(1))) == (403, "ACCESS_FORBIDDEN")


# --- AC3: the limits of each role ----------------------------------------------------------------


def limited(server: Server, role: str, limit: RoleLimit) -> None:
    QueryLimits(server.store, server.audit).change(server.admin, {role: limit})


def test_a_question_beyond_the_roles_concurrency_is_refused_then_served(server: Server) -> None:
    ana = server.author("ana")
    limited(server, "author", RoleLimit(1, 60_000))
    held = server.semantic.slots.take(ana, "project-1", 1)  # a question of ana's runs
    refused = ask(server.client("ana"), detail(1))
    assert code(refused) == (429, "QUERY_BUDGET_EXCEEDED")
    assert refused.headers["Retry-After"]
    assert refused.json()["error"]["semantic"]["details"]["budget"] == "concurrent_queries"
    assert "rows" not in refused.text
    held.release()
    assert ask(server.client("ana"), detail(1)).status_code == 200
    assert server.semantic.slots.running(ana, "project-1") == 0


def test_the_limit_is_per_user_and_reads_the_settings_now(server: Server) -> None:
    ana, ben = server.author("ana"), server.author("ben")
    held = server.semantic.slots.take(ana, "project-1", 2)
    other = server.semantic.slots.take(ana, "project-1", 2)
    assert code(ask(server.client("ana"), detail(1)))[0] == 429  # the default: 2
    assert ask(server.client("ben"), detail(1)).status_code == 200  # ben's own slots
    limited(server, "author", RoleLimit(3, 60_000))
    assert ask(server.client("ana"), detail(1)).status_code == 200
    held.release(), other.release()
    assert server.semantic.slots.running(ana, "project-1") == 0
    assert server.semantic.slots.running(ben, "project-1") == 0


def test_the_slot_is_released_whatever_ends_the_question(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana")
    limited(server, "author", RoleLimit(1, 60_000))  # a slot left held would refuse the next
    http = server.client("ana")
    unknown = {**AGGREGATE, "metrics": [{"metric": "nothing"}]}
    assert code(ask(http, unknown)) == (404, "MEMBER_NOT_FOUND")
    assert ask(http, detail(1)).status_code == 200
    assert ask(http, detail(1)).status_code == 200  # a hit

    def broken(self: Any) -> Any:  # noqa: ANN401
        raise RuntimeError("the response could not be built")

    timeouts = answer

    def timed_out(*args: Any) -> Any:  # noqa: ANN401
        raise SemanticError("QUERY_BUDGET_EXCEEDED", {"budget": "time_ms", "limit": "1"})

    monkeypatch.setattr(semantic_http, "answer", timed_out)
    assert code(ask(http, detail(3))) == (429, "QUERY_BUDGET_EXCEEDED")
    monkeypatch.setattr(semantic_http, "answer", timeouts)
    withdrawing = semantic_delivery.deliver

    def withdrawn(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise SemanticError("QUERY_RESULT_WITHDRAWN", {"reason": "permissions_changed"})

    monkeypatch.setattr(semantic_delivery, "deliver", withdrawn)
    assert code(ask(http, detail(4))) == (409, "QUERY_RESULT_WITHDRAWN")
    monkeypatch.setattr(semantic_delivery, "deliver", withdrawing)
    monkeypatch.setattr(semantic_http.Asked, "json_document", broken)
    try:
        failed = ask(http, detail(2)).status_code
    except RuntimeError:
        failed = 500
    assert failed == 500
    assert server.semantic.slots.running(ana, "project-1") == 0
    monkeypatch.undo()
    limited(server, "author", RoleLimit(2, 60_000))
    answers: list[int] = []
    clients = [server.client("ana") for _ in range(2)]
    threads = [
        threading.Thread(target=lambda c=c: answers.append(ask(c, detail(5)).status_code))
        for c in clients
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert answers == [200, 200]  # max_concurrent at once, every slot back


@pytest.mark.parametrize("role", ["builder", "author"])
def test_the_roles_run_time_bounds_the_engines(
    server: Server, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    seen: list[int | None] = []
    original = answer

    def spied(body: Any, compiled: Any, policies: Any, context: Any, engine: Any) -> Any:  # noqa: ANN401
        seen.append(engine.timeout_ms)
        return original(body, compiled, policies, context, engine)

    monkeypatch.setattr(semantic_http, "answer", spied)
    name = "builder" if role == "builder" else "ana"
    if role == "author":
        server.author("ana")
    assert ask(server.client(name), detail(1)).status_code == 200
    limited(server, role, RoleLimit(2, 1_500))
    assert ask(server.client(name), detail(2)).status_code == 200
    default = 120_000 if role == "builder" else 60_000
    assert seen == [
        min(semantic_http.QUERY_TIMEOUT_MS, default), min(semantic_http.QUERY_TIMEOUT_MS, 1_500)
    ]  # fmt: skip


def test_no_limit_cuts_the_rows_of_an_answer(server: Server) -> None:
    server.author("ana", "auditors")  # exempt from the row policy: every row
    whole = ask(server.client("ana"), detail(1000)).json()["result"]["rows"]
    limited(server, "author", RoleLimit(1, 1_000))
    answer = ask(server.client("ana"), detail(1000))
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == whole
    assert len(whole) > 2


# --- AC8: the audit log is mandatory --------------------------------------------------------------


def test_server_mode_does_not_start_without_its_audit_log(server: Server) -> None:
    unaudited = replace(server.semantic, audit=None)
    with pytest.raises(ValueError, match="audit"):
        create_server_app(
            VERSIONS,
            server.sign_in,
            server.settings,
            IDENTITY.slug,
            served=ServedProject("project-1", unaudited, server.store),
        )


def test_server_mode_does_not_start_when_its_audit_log_does_not_verify(server: Server) -> None:
    anchors = server.audit.anchors
    lines = anchors.read_text("ascii").splitlines()
    assert lines  # the bootstrap's checkpoints were anchored
    last = lines[-1]
    forged = last[:-1] + ("0" if last[-1] != "0" else "1")  # a checkpoint that never was
    anchors.write_text("\n".join([*lines[:-1], forged]) + "\n", "ascii")
    with pytest.raises(AccessError):
        create_server_app(
            VERSIONS,
            server.sign_in,
            server.settings,
            IDENTITY.slug,
            served=ServedProject("project-1", server.semantic, server.store),
        )


@pytest.mark.parametrize("missing", ["identity", "limits"])
def test_server_mode_does_not_start_with_local_modes_identity_or_limits(
    server: Server, missing: str
) -> None:
    if missing == "identity":
        composed = replace(server.semantic, identity=semantic_http.local_identity)
    else:
        composed = replace(server.semantic, limits=semantic_http.local_limits)
    with pytest.raises(ValueError, match="identity and limits"):
        create_server_app(
            VERSIONS,
            server.sign_in,
            server.settings,
            IDENTITY.slug,
            served=ServedProject("project-1", composed, server.store),
        )
