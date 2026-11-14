"""#445 AC6 and AC7 over HTTP in server mode: the one delivery step decides after the answer is
computed, for a read (`miss`), a cache `hit` and a `bypass` alike. A revocation committed before
it withdraws the answer and nothing of it is sent; the `query` event is recorded once per
delivery; an event that cannot be recorded refuses the answer with `503`, except a withheld
answer's (#443)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from orvex.access.errors import ACCESS_FORBIDDEN, AUDIT_UNAVAILABLE, AccessError
from orvex.access.server_context import ServerRequest
from orvex.access.storage.store import TransientConflict
from orvex.semantic.errors import SemanticError
from orvex.server import semantic_delivery, semantic_http
from orvex.server.semantic_audit import record_query

from tests.support.access_directory import audit_events
from tests.support.semantic_http import detail, extension, model, run, write

from .test_server_semantic import MAPPED, SAME, Server, ask


def code(answer: Any) -> tuple[int, str]:  # noqa: ANN401
    return answer.status_code, answer.json()["error"]["code"]


def cache(answer: Any) -> str:  # noqa: ANN401
    return str(answer.json()["receipt"]["cache"])


def queries(server: Server) -> list[dict[str, Any]]:
    return [e for e in audit_events(server.store) if e["kind"] == "query"]


@pytest.fixture(params=["miss", "hit", "bypass"])
def origin(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def server(tmp_path: Path, origin: str) -> Server:
    return composed(tmp_path, origin)


def composed(tmp_path: Path, origin: str, policies: str | None = None) -> Server:
    built = Server(tmp_path) if policies is None else Server(tmp_path, policies)
    if origin == "bypass":  # the dataset turns the cache off
        document = json.loads(model())
        document["datasets"][0]["custom_extensions"] = [
            extension({"element": "dataset", "cache": {"enabled": False}})
        ]
        write(tmp_path, {"semantic/readings.ossie.json": json.dumps(document)})
        run(tmp_path)
    return built


def revoked_after_asking(monkeypatch: pytest.MonkeyPatch, revoke: Callable[[], None]) -> None:
    """The revocation commits after the answer is computed, before the delivery step."""
    original = semantic_http.ask

    def asked_then_revoked(semantic: Any, body: bytes, request: object) -> Any:  # noqa: ANN401
        found = original(semantic, body, request)
        revoke()
        return found

    monkeypatch.setattr(semantic_http, "ask", asked_then_revoked)


def primed(server: Server, origin: str, http: Any) -> None:  # noqa: ANN401
    """For a hit, the same question answered once before."""
    if origin == "hit":
        assert cache(ask(http, detail(10))) == "miss"


def test_an_attribute_changed_before_delivery_withdraws_the_answer(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    before = len(queries(server))
    revoked_after_asking(
        monkeypatch,
        lambda: server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "x"),
    )
    withdrawn = ask(http, detail(10))
    assert code(withdrawn) == (409, "QUERY_RESULT_WITHDRAWN")
    assert "rows" not in withdrawn.text
    assert "receipt" not in withdrawn.json()
    assert len(queries(server)) == before  # nothing was delivered
    assert server.semantic.slots.running(ana, "project-1") == 0
    refusal = [e for e in audit_events(server.store) if e["kind"] == "access.refused"][-1]
    assert refusal["details"] == {"operation": "delivery", "code": "QUERY_RESULT_WITHDRAWN"}
    assert (refusal["actor"], refusal["project"]) == (ana, "project-1")


def test_a_mapping_row_removed_before_delivery_withdraws_the_answer(
    tmp_path: Path, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-REVOKE-002's revocation, for each origin: the mapping row that granted the rows is
    removed after the answer is computed."""
    mapped = composed(tmp_path, origin, MAPPED)
    ana = mapped.author("ana", tariff=None)
    mapped.mapping({ana: ["residential", "commercial"]})
    run(mapped.folder)
    http = mapped.client("ana")
    if origin == "hit":
        assert cache(ask(http, detail(10))) == "miss"

    def removed() -> None:
        mapped.mapping({ana: ["residential"]})
        run(mapped.folder)

    revoked_after_asking(monkeypatch, removed)
    withdrawn = ask(http, detail(10))
    assert code(withdrawn) == (409, "QUERY_RESULT_WITHDRAWN")
    assert "rows" not in withdrawn.text
    monkeypatch.undo()
    again = ask(http, detail(10))  # the permissions in force now: residential only
    assert [row[0] for row in again.json()["result"]["rows"]] == [1, 2]


def test_a_group_removed_before_delivery_ends_the_session_and_sends_nothing(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    revoked_after_asking(
        monkeypatch,
        lambda: server.directory.remove_group_member(server.builder, "project-1", "finance", ana),
    )
    refused = ask(http, detail(10))
    assert code(refused) == (401, "SESSION_EXPIRED")
    assert "rows" not in refused.text


def test_a_role_lowered_before_delivery_is_forbidden(server: Server, origin: str) -> None:
    """Without the session's recheck (as the step runs for any caller): the role read now no
    longer explores."""
    ana = server.author("ana", "finance")
    request = ServerRequest(ana, "project-1")
    body = json.dumps(detail(10)).encode()
    if origin == "hit":
        semantic_http.ask(server.semantic, body, request)
    asked = semantic_http.ask(server.semantic, body, request)
    assert asked.receipt["cache"] == origin
    server.directory.change_role(server.builder, "project-1", ana, "viewer")
    with pytest.raises(AccessError) as caught:
        semantic_delivery.deliver(server.semantic, request, asked, "synchronous")
    assert caught.value.code == ACCESS_FORBIDDEN


def test_a_membership_removed_before_delivery_refuses(server: Server, origin: str) -> None:
    ana = server.author("ana", "finance")
    request = ServerRequest(ana, "project-1")
    body = json.dumps(detail(10)).encode()
    if origin == "hit":
        semantic_http.ask(server.semantic, body, request)
    asked = semantic_http.ask(server.semantic, body, request)
    server.directory.remove_membership(server.builder, "project-1", ana)
    with pytest.raises((AccessError, SemanticError)):
        semantic_delivery.deliver(server.semantic, request, asked, "synchronous")


def test_an_unchanged_caller_is_delivered_and_audited_once(server: Server, origin: str) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    before = len(queries(server))
    answer = ask(http, detail(10))
    assert answer.status_code == 200, answer.text
    assert cache(answer) == origin
    (event,) = queries(server)[before:]
    assert event["details"] == {
        "receipt": answer.json()["receipt"]["receipt_id"],
        "cache": origin,
        "mode": "detail",
        "stage": "synchronous",
    }


def test_an_unrecordable_query_event_refuses_the_answer(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    original = server.audit.record_alone

    def refused(kind: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if kind == "query":
            raise AccessError(AUDIT_UNAVAILABLE, "The audit log cannot record.")
        original(kind, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record_alone", refused)
    unrecorded = server.audit.unrecorded
    answer = ask(http, detail(10))
    assert code(answer) == (503, AUDIT_UNAVAILABLE)
    assert "rows" not in answer.text
    assert server.audit.unrecorded == unrecorded  # refused, not counted: the exception is #443's
    assert server.semantic.slots.running(ana, "project-1") == 0


def test_an_empty_answer_that_is_not_withheld_still_needs_its_event(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero rows by the policy's own predicate (no attribute or mapping missing): not a
    withheld answer, so an unrecordable event is still `503` (precision 5)."""
    server.author("ana", "finance", tariff="no-such-tariff")
    http = server.client("ana")
    primed(server, origin, http)

    def refused(kind: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        raise AccessError(AUDIT_UNAVAILABLE, "The audit log cannot record.")

    monkeypatch.setattr(server.audit, "record_alone", refused)
    assert code(ask(http, detail(10))) == (503, AUDIT_UNAVAILABLE)


def test_catalog_and_explain_carry_no_rows_and_no_query_event(server: Server, origin: str) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    assert http.get("/api/v1/semantic/catalog").status_code == 200
    explained = http.post("/api/v1/semantic/explain", json=detail(10), headers={
        "Origin": "https://bi.example.com", "Sec-Fetch-Site": "same-origin"})  # fmt: skip
    assert explained.status_code == 200, explained.text
    assert queries(server) == []


@pytest.mark.parametrize("accept", ["application/json", semantic_http.ARROW])
def test_a_withdrawn_response_is_refused_whole(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch, accept: str
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    answer = ask(http, detail(10))
    shown = {str(row[1]) for row in answer.json()["result"]["rows"]} - {"None"}
    revoked_after_asking(
        monkeypatch,
        lambda: server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "x"),
    )
    withdrawn = http.post(
        "/api/v1/semantic/query", json=detail(10), headers={**SAME, "Accept": accept}
    )
    assert withdrawn.headers["content-type"].startswith("application/json")  # never Arrow
    assert code(withdrawn) == (409, "QUERY_RESULT_WITHDRAWN")
    assert set(withdrawn.json()) == {"error"}
    assert not any(value in withdrawn.text for value in shown)


def test_a_revocation_after_the_delivery_step_applies_to_the_next_request(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    recording = record_query
    revoked: list[bool] = []

    def recorded_then_revoked(*args: Any) -> None:  # noqa: ANN401
        recording(*args)
        if not revoked:
            revoked.append(True)
            server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "x")

    monkeypatch.setattr(semantic_delivery, "record_query", recorded_then_revoked)
    delivered = ask(http, detail(10))
    assert delivered.status_code == 200  # decided before the revocation
    after = ask(http, detail(10))
    assert after.status_code == 200
    assert after.json()["result"]["rows"] == []  # the permissions in force now


def test_a_retried_recording_leaves_one_event(
    server: Server, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store gives up on the event's first attempt (a serialization conflict: nothing of
    it committed) and the log writes it again: one event, never two."""
    server.author("ana", "finance")
    http = server.client("ana")
    primed(server, origin, http)
    before = len(queries(server))
    original = server.audit.record
    conflicts: list[str] = []

    def conflicting(transaction: Any, kind: str, *args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        if kind == "query" and not conflicts:
            conflicts.append(kind)
            raise TransientConflict("40001")
        return original(transaction, kind, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", conflicting)
    answer = ask(http, detail(10))
    assert answer.status_code == 200, answer.text
    assert conflicts == ["query"]
    (event,) = queries(server)[before:]
    assert event["details"]["receipt"] == answer.json()["receipt"]["receipt_id"]
    assert server.audit.verify() >= 1
