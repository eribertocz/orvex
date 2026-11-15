"""The server's end-to-end result-cache suite (#444, #486 G1): over HTTP, with whichever result
cache the server is assembled with, answers, error codes, receipts (`cache: hit`, `miss`,
`bypass`), cursors, expiry, eviction under the bound, and two callers with different
permissions never sharing an entry. The real substitution test runs it once per
implementation and compares the runs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from orvex.server import semantic_http

from tests.support import semantic_http as harness

from .test_server_semantic import Server
from .test_server_semantic import ask as ask_server


class Clock:
    """The monotonic clock the server's cache is built on in this suite."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    found = Clock()
    monkeypatch.setattr(semantic_http, "time", SimpleNamespace(monotonic=found))
    return found


def cache(answer: Any) -> str:  # noqa: ANN401
    assert answer.status_code == 200, answer.text
    return str(answer.json()["receipt"]["cache"])


def rows(answer: Any) -> Any:  # noqa: ANN401
    return answer.json()["result"]["rows"]


def with_cache(folder: Path, settings: dict[str, object]) -> None:
    """The project's model with the `readings` dataset's `cache` extension set."""
    model = json.loads(harness.model())
    model["datasets"][0]["custom_extensions"] = [
        harness.extension({"element": "dataset", "cache": settings})
    ]
    harness.write(folder, {"semantic/readings.ossie.json": json.dumps(model)})


def local(folder: Path, cache_settings: dict[str, object] | None = None) -> Any:  # noqa: ANN401
    harness.project(folder)
    if cache_settings is not None:
        with_cache(folder, cache_settings)
    harness.run(folder)
    semantic = harness.local(folder)
    http = harness.client(semantic)
    return lambda query: harness.ask(http, semantic, query)


def test_a_repeated_question_is_a_hit_with_the_same_answer(tmp_path: Path, clock: Clock) -> None:
    ask = local(tmp_path)
    first, second = ask(harness.aggregate()), ask(harness.aggregate())
    assert (cache(first), cache(second)) == ("miss", "hit")
    assert rows(first) == rows(second)
    assert first.json()["receipt"]["receipt_id"] != second.json()["receipt"]["receipt_id"]


def test_a_refusal_keeps_its_code_and_is_never_stored(tmp_path: Path, clock: Clock) -> None:
    ask = local(tmp_path)
    query = {**harness.aggregate(), "metrics": [{"metric": "no_such_metric"}]}
    refusals = [ask(query) for _ in range(2)]
    assert [r.status_code for r in refusals] == [404, 404]
    assert {r.json()["error"]["semantic"]["code"] for r in refusals} == {"MEMBER_NOT_FOUND"}
    assert cache(ask(harness.aggregate())) == "miss"


def test_a_dataset_with_the_cache_off_is_bypassed(tmp_path: Path, clock: Clock) -> None:
    ask = local(tmp_path, {"enabled": False})
    answers = [ask(harness.aggregate()) for _ in range(2)]
    assert [cache(a) for a in answers] == ["bypass", "bypass"]
    assert rows(answers[0]) == rows(answers[1])


def test_an_entry_expires_at_its_ttl(tmp_path: Path, clock: Clock) -> None:
    ask = local(tmp_path, {"enabled": True, "ttl": "PT10S"})
    assert cache(ask(harness.aggregate())) == "miss"
    clock.now += 9.5
    assert cache(ask(harness.aggregate())) == "hit"
    clock.now += 0.5
    assert cache(ask(harness.aggregate())) == "miss"
    assert cache(ask(harness.aggregate())) == "hit"


def test_the_least_recently_used_answer_is_evicted_under_the_bound(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_http, "RESULT_CACHE_BYTES", 16 * 1300)
    ask = local(tmp_path)
    sizes = range(1, 31)  # more distinct answers than the bound holds
    assert [cache(ask(harness.detail(size))) for size in sizes] == ["miss"] * len(sizes)
    assert cache(ask(harness.detail(sizes[-1]))) == "hit"
    assert cache(ask(harness.detail(sizes[0]))) == "miss"  # the least recently used left


def test_a_hit_issues_its_own_cursor_to_the_same_next_page(tmp_path: Path, clock: Clock) -> None:
    ask = local(tmp_path)
    first, second = ask(harness.detail(2)), ask(harness.detail(2))
    assert (cache(first), cache(second)) == ("miss", "hit")
    cursors = [a.json()["result"]["page"]["next"] for a in (first, second)]
    assert cursors[0] != cursors[1]
    pages = [ask(harness.detail(2, after)) for after in cursors]
    assert all(page.status_code == 200 for page in pages), pages[0].text
    assert rows(pages[0]) == rows(pages[1])
    assert rows(pages[0]) != rows(first)


def test_two_callers_with_different_permissions_never_share_an_entry(
    tmp_path: Path, clock: Clock
) -> None:
    server = Server(tmp_path)
    server.author("ana", "finance")
    server.author("ben")
    ana, ben = server.client("ana"), server.client("ben")
    assert cache(ask_server(ana, harness.detail(10))) == "miss"
    assert cache(ask_server(ana, harness.detail(10))) == "hit"
    first, second = ask_server(ben, harness.detail(10)), ask_server(ben, harness.detail(10))
    assert (cache(first), cache(second)) == ("miss", "hit")
    assert rows(first) != rows(ask_server(ana, harness.detail(10)))
