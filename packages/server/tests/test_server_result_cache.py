"""#444 AC7 and AC8c over HTTP in server mode: the result cache answers the second identical
question of a caller (`cache: hit` in the receipt), still through the same response step as a
read answer, so a hit is audited like a miss (#443) and a revocation between two requests is
refused before any lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from orvex.server import semantic_delivery
from orvex.server.semantic_audit import record_withheld as recording

from tests.support.access_directory import audit_events
from tests.support.semantic_http import aggregate, detail

from .test_server_semantic import Server, ask

KINDS = ("policy.attribute_missing", "policy.mapping_missing")


def cache(answer: Any) -> str:  # noqa: ANN401
    return str(answer.json()["receipt"]["cache"])


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def test_the_second_identical_question_is_a_hit_with_its_own_receipt(server: Server) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    first, second = ask(http, aggregate()), ask(http, aggregate())
    assert first.status_code == second.status_code == 200, second.text
    assert (cache(first), cache(second)) == ("miss", "hit")
    assert first.json()["result"]["rows"] == second.json()["result"]["rows"]
    assert first.json()["receipt"]["receipt_id"] != second.json()["receipt"]["receipt_id"]


def test_a_hit_goes_through_the_response_step_and_is_audited(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response step (here the #443 recording, standing in for every check #445 adds
    there) runs for a hit exactly as for a miss."""
    server.author("ben", "finance", tariff=None)
    http = server.client("ben")
    calls: list[str] = []

    def counted(audit: Any, found: Any, context: Any) -> None:  # noqa: ANN401
        calls.append(str(found.receipt.document()["execution"]["cache"]))
        recording(audit, found, context)

    monkeypatch.setattr(semantic_delivery, "record_withheld", counted)
    answers = [ask(http, detail(10)) for _ in range(2)]
    assert [a.status_code for a in answers] == [200, 200]
    assert calls == ["miss", "hit"]
    events = [e for e in audit_events(server.store) if e["kind"] in KINDS]
    assert [e["details"]["receipt"] for e in events] == [
        a.json()["receipt"]["receipt_id"] for a in answers
    ]


def test_a_revocation_between_two_requests_is_refused_whatever_was_cached(
    server: Server,
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    assert ask(http, aggregate()).status_code == 200
    assert cache(ask(http, aggregate())) == "hit"
    server.directory.remove_membership(server.builder, "project-1", ana)
    refused = ask(http, aggregate())
    assert refused.status_code in {401, 403, 404}, refused.text
    assert "rows" not in refused.text


def test_a_lost_group_misses_and_answers_for_the_permissions_now(server: Server) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    before = ask(http, detail(10))
    server.directory.remove_group_member(server.builder, "project-1", "finance", ana)
    after = ask(server.client("ana"), detail(10))  # the change ended the session: sign in
    assert after.status_code == 200, after.text
    assert cache(after) == "miss"
    assert after.json()["result"]["rows"] != before.json()["result"]["rows"] or (
        after.json()["result"]["columns"] != before.json()["result"]["columns"]
    )
