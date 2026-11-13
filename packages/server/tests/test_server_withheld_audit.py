"""#443 AC4, AC5, AC6 over HTTP in server mode: an answer whose row policy withheld every row
records why in the audit log, before it is returned, as `policy.attribute_missing` or
`policy.mapping_missing` with the dataset, the policy and the receipt; nothing of the event
reaches the caller; when the log cannot record, the empty answer is still returned, the failure
counted and logged once, naming nothing protected."""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from orvex.access.errors import AUDIT_UNAVAILABLE, AccessError
from orvex.server import semantic_http

from tests.support.access_directory import audit_events
from tests.support.semantic_http import aggregate, detail

from .test_server_semantic import MAPPED, SAME, Server, ask

SEPARATOR = re.compile(r"[^A-Za-z0-9_.-]+")
SECRET = {"by_tariff", "mapped_tariffs", "policy.attribute_missing",
          "policy.mapping_missing", "security.user_tariff", "user_tariff"}  # fmt: skip


def events(server: Server, *kinds: str) -> list[dict[str, Any]]:
    return [e for e in audit_events(server.store) if e["kind"] in kinds]


KINDS = ("policy.attribute_missing", "policy.mapping_missing")


def tokens(response: Any) -> set[str]:  # noqa: ANN401
    """Every token the caller receives: the body (JSON or Arrow, with its schema metadata),
    and the headers."""
    texts = [f"{k}: {v}" for k, v in response.headers.items()]
    if response.headers["content-type"] == semantic_http.ARROW:
        table = pa.ipc.open_stream(io.BytesIO(response.content)).read_all()
        texts += [v.decode() for v in (table.schema.metadata or {}).values()]
        texts += [json.dumps(table.to_pylist(), default=str)]
    else:
        texts.append(response.text)
    return {t for text in texts for t in SEPARATOR.split(text)}


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


@pytest.mark.parametrize("accept", ["application/json", semantic_http.ARROW])
def test_a_missing_attribute_records_one_event_and_the_caller_sees_none_of_it(
    server: Server, accept: str
) -> None:
    ben = server.viewer("ben", "finance", tariff=None)
    answer = ask_as(server.client("ben"), detail(10), accept)
    assert answer.status_code == 200, answer.text
    found = events(server, *KINDS)
    assert len(found) == 1
    (event,) = found
    assert (event["kind"], event["actor"], event["project"], event["outcome"]) == (
        "policy.attribute_missing", ben, "project-1", "refused",
    )  # fmt: skip
    receipt = receipt_of(answer)
    assert event["details"] == {"dataset": "readings", "policy": "by_tariff", "receipt": receipt}
    assert not SECRET & tokens(answer)  # the generic sentence only
    assert event["id"] not in tokens(answer)
    assert server.audit.verify() >= 1  # the chain holds after the event


def ask_as(http: Any, query: dict[str, Any], accept: str = "application/json") -> Any:  # noqa: ANN401
    return http.post("/api/v1/semantic/query", json=query, headers={**SAME, "Accept": accept})


def receipt_of(answer: Any) -> str:  # noqa: ANN401
    if answer.headers["content-type"] == semantic_http.ARROW:
        table = pa.ipc.open_stream(io.BytesIO(answer.content)).read_all()
        envelope = json.loads((table.schema.metadata or {})[semantic_http.ENVELOPE_KEY])
    else:
        envelope = answer.json()["result"]
    return str(envelope["receipt"]["receipt_id"])


def test_each_page_and_question_records_its_own_event(server: Server) -> None:
    server.viewer("ben", "finance", tariff=None)
    http = server.client("ben")
    first, second = ask(http, detail(1)), ask(http, aggregate())
    assert first.status_code == second.status_code == 200
    found = events(server, *KINDS)
    assert [e["details"]["receipt"] for e in found] == [receipt_of(first), receipt_of(second)]


def test_no_event_for_a_caller_with_the_attribute_an_explanation_or_an_exemption(
    server: Server,
) -> None:
    server.viewer("ana", "finance")
    server.viewer("ben", tariff=None)
    server.viewer("cid", "auditors", tariff=None)  # exempt: the policy does not apply
    assert ask(server.client("ana"), detail(10)).status_code == 200
    explained = server.client("ben").post("/api/v1/semantic/explain", json=detail(10), headers=SAME)
    assert explained.status_code == 200, explained.text
    assert ask(server.client("cid"), detail(10)).status_code == 200
    assert events(server, *KINDS) == []


def test_a_builder_sees_the_policy_in_the_notice_and_nothing_of_the_event(server: Server) -> None:
    # The project's Builder has no `tariff` attribute.
    answer = ask(server.client("builder"), detail(10))
    assert answer.status_code == 200, answer.text
    assert [n["details"] for n in answer.json()["result"]["notices"]] == [{"policy": "by_tariff"}]
    (event,) = events(server, *KINDS)
    received = tokens(answer)
    assert not {"policy.attribute_missing", event["id"]} & received


def test_a_missing_mapping_records_its_event(tmp_path: Path) -> None:
    server = Server(tmp_path, MAPPED)
    dee = server.viewer("dee", tariff=None)
    answer = ask(server.client("dee"), aggregate())
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["rows"] == []
    (event,) = events(server, *KINDS)
    assert (event["kind"], event["actor"]) == ("policy.mapping_missing", dee)
    assert event["details"]["policy"] == "mapped_tariffs"
    assert not SECRET & tokens(answer)


def test_an_unrecordable_event_still_returns_the_empty_answer_and_is_counted(
    server: Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server.viewer("ben", "finance", tariff=None)
    http = server.client("ben")
    working = ask(http, detail(10)).json()

    def refused(*_: object, **__: object) -> str:
        raise AccessError(AUDIT_UNAVAILABLE, "simulated", {})

    monkeypatch.setattr(server.audit, "record_alone", refused)
    before = server.audit.unrecorded
    with caplog.at_level(logging.ERROR):
        failing = ask(http, detail(10))
    assert failing.status_code == 200, failing.text
    body = failing.json()
    for document in (working, body):  # the same answer, its receipt apart
        document["result"].pop("receipt")
        document.pop("receipt")
    assert body == working
    assert server.audit.unrecorded == before + 1
    lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert lines == [
        "audit: a policy.attribute_missing event of project project-1 could not be recorded"
    ]
    assert not {"ben", "readings", "by_tariff", "$tariff"} & {
        t for line in lines for t in SEPARATOR.split(line)
    }
