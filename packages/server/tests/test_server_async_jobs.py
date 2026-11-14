"""#445 AC4, AC5, AC3b and AC7 over HTTP in server mode: an asynchronous question (`Prefer:
respond-async`) answers as the synchronous one does, only to its owner; its execution is
audited whether or not anyone fetches it, each fetch is audited as a delivery, and a revocation
between the start and the fetch withdraws the answer, nothing of it sent (SEC-REVOKE-002). Its
slot is held while it runs, a cancel included, and released once it stops."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from orvex.access.authentication.query_limits import QueryLimits, RoleLimit
from orvex.access.errors import AUDIT_UNAVAILABLE, AccessError
from orvex.semantic.execution import answer
from orvex.server import semantic_delivery, semantic_http, semantic_jobs
from orvex.server.query_cancel import Cancel
from orvex.server.semantic_audit import record_query

from tests.support.access_directory import audit_events
from tests.support.semantic_http import detail, run

from .test_server_semantic import MAPPED, SAME, Server, ask

ASYNC = {**SAME, "Prefer": "respond-async"}
JOBS = "/api/v1/semantic/jobs/"


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


@pytest.fixture
def mapped(tmp_path: Path) -> Server:
    return Server(tmp_path, MAPPED)


def start(http: Any, query: dict[str, Any]) -> str:  # noqa: ANN401
    started = http.post("/api/v1/semantic/query", json=query, headers=ASYNC)
    assert started.status_code == 202, started.text
    job = str(started.json()["job"])
    assert started.json()["status"] == "running"
    assert started.headers["Location"] == JOBS + job
    assert len(job) == 32
    assert int(job, 16) >= 0
    return job


def until(done: Callable[[], bool], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def fetched(http: Any, job: str) -> Any:  # noqa: ANN401
    """The job's answer once it has stopped running."""
    found: list[Any] = []

    def stopped() -> bool:
        answer = http.get(JOBS + job)
        found[:] = [answer]
        return bool(answer.status_code != 200 or answer.json()["status"] != "running")

    until(stopped)
    return found[0]


def queries(server: Server) -> list[dict[str, Any]]:
    return [e for e in audit_events(server.store) if e["kind"] == "query"]


def stages(server: Server) -> list[tuple[str, str | None]]:
    """The `query` events of answers: each with its receipt."""
    found = [e for e in queries(server) if "end" not in e["details"]]
    return [(e["details"]["stage"], e["details"].get("job")) for e in found]


def ended(server: Server) -> list[tuple[str, str]]:
    """The `executed` events of jobs that ended with no answer: no receipt, their end."""
    found = [e["details"] for e in queries(server) if "end" in e["details"]]
    assert all(set(d) == {"stage", "job", "end"} and d["stage"] == "executed" for d in found)
    return [(d["job"], d["end"]) for d in found]


def code(answer: Any) -> tuple[int, str]:  # noqa: ANN401
    return answer.status_code, answer.json()["error"]["code"]


# --- AC4: the same answer, to its owner only ------------------------------------------------------


def test_an_asynchronous_answer_is_the_synchronous_one(server: Server) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    answer = fetched(http, job)
    assert answer.status_code == 200, answer.text
    assert (answer.json()["job"], answer.json()["status"]) == (job, "done")
    synchronous = ask(http, detail(10)).json()
    for part in ("rows", "columns", "notices", "cell_states", "query_fingerprint"):
        assert answer.json()["answer"]["result"][part] == synchronous["result"][part]


def test_an_answer_withheld_asynchronously_is_the_synchronous_one(server: Server) -> None:
    server.author("ben", "finance", tariff=None)
    http = server.client("ben")
    answer = fetched(http, start(http, detail(10)))
    synchronous = ask(http, detail(10)).json()
    assert answer.json()["answer"]["result"]["rows"] == synchronous["result"]["rows"] == []
    assert answer.json()["answer"]["result"]["notices"] == synchronous["result"]["notices"]


def test_the_execution_fingerprint_is_the_synchronous_one(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprints: list[str] = []
    original = answer

    def kept(*args: Any) -> Any:  # noqa: ANN401
        found = original(*args)
        fingerprints.append(found.receipt.document()["execution_fingerprint"])
        return found

    monkeypatch.setattr(semantic_http, "answer", kept)
    server.author("ana", "finance")
    http = server.client("ana")
    fetched(http, start(http, detail(10)))
    ask(http, detail(10))
    assert len(fingerprints) == 2
    assert fingerprints[0] == fingerprints[1]


def test_a_failed_job_gives_the_synchronous_error(server: Server) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    unknown = {**detail(10), "fields": [{"member": "readings.nothing"}]}
    synchronous = ask(http, unknown)
    failed = fetched(http, start(http, unknown))
    assert code(failed) == code(synchronous)
    assert failed.json()["error"]["semantic"] == synchronous.json()["error"]["semantic"]


def test_an_answer_is_its_owners_alone(server: Server) -> None:
    server.author("ana", "finance")
    server.author("ben", "finance")
    job = start(server.client("ana"), detail(10))
    fetched(server.client("ana"), job)
    ben = server.client("ben")
    unknown = "0" * 32
    for path in (JOBS + job, JOBS + unknown):
        assert code(ben.get(path)) == (404, "SEMANTIC_JOB_NOT_FOUND")
        assert code(ben.delete(path, headers=SAME)) == (404, "SEMANTIC_JOB_NOT_FOUND")
    assert "rows" not in ben.get(JOBS + job).text
    assert fetched(server.client("ana"), job).status_code == 200  # still hers


def test_the_execution_is_audited_unfetched_and_each_fetch_as_a_delivery(server: Server) -> None:
    server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(server))
    assert stages(server) == [("executed", job)]  # nobody has fetched it
    first = fetched(http, job)
    second = fetched(http, job)
    assert stages(server) == [("executed", job), ("delivered", job), ("delivered", job)]
    receipts = {e["details"]["receipt"] for e in queries(server)}
    assert receipts == {first.json()["answer"]["receipt"]["receipt_id"]}
    assert second.json()["answer"] == first.json()["answer"]
    assert {e["outcome"] for e in queries(server)} == {"allowed"}


def test_a_synchronous_answer_is_audited_once(server: Server) -> None:
    server.author("ana", "finance")
    answer = ask(server.client("ana"), detail(10))
    (event,) = queries(server)
    assert event["details"]["stage"] == "synchronous"
    assert "job" not in event["details"]
    assert event["details"]["receipt"] == answer.json()["receipt"]["receipt_id"]
    assert (event["details"]["mode"], event["details"]["cache"]) == ("detail", "miss")


def test_an_unrecordable_execution_fails_the_job_and_frees_its_slot(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    original = server.audit.record_alone

    def refused(kind: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if kind == "query":
            raise AccessError(AUDIT_UNAVAILABLE, "The audit log cannot record.")
        original(kind, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record_alone", refused)
    job = start(http, detail(10))
    failed = fetched(http, job)
    assert code(failed) == (503, AUDIT_UNAVAILABLE)
    assert "rows" not in failed.text
    until(lambda: server.semantic.slots.running(ana, "project-1") == 0)


# --- AC5: a revocation before the fetch -----------------------------------------------------------


def test_a_mapping_removed_before_the_fetch_withdraws_the_answer(mapped: Server) -> None:
    """SEC-REVOKE-002 over HTTP: the mapping that granted the rows is removed while the job's
    answer waits; the fetch is refused and nothing of the answer is sent."""
    ana = mapped.author("ana", tariff=None)
    mapped.mapping({ana: ["residential", "commercial"]})
    run(mapped.folder)
    http = mapped.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(mapped))
    mapped.mapping({ana: ["residential"]})
    run(mapped.folder)
    withdrawn = fetched(http, job)
    assert code(withdrawn) == (409, "QUERY_RESULT_WITHDRAWN")
    assert withdrawn.json()["error"]["semantic"]["details"] == {"reason": "permissions_changed"}
    assert "rows" not in withdrawn.text
    assert "answer" not in withdrawn.json()
    assert stages(mapped) == [("executed", job)]  # no delivery was made
    (refusal,) = [e for e in audit_events(mapped.store) if e["kind"] == "access.refused"]
    assert refusal["details"] == {"operation": "delivery", "code": "QUERY_RESULT_WITHDRAWN"}
    assert (refusal["actor"], refusal["outcome"]) == (ana, "refused")
    again = ask(http, detail(10))  # asked again: the permissions in force now
    assert [row[0] for row in again.json()["result"]["rows"]] == [1, 2]


def test_an_attribute_changed_before_the_fetch_withdraws_the_answer(server: Server) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(server))
    server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "commercial")
    assert code(fetched(http, job)) == (409, "QUERY_RESULT_WITHDRAWN")


def test_a_group_removed_before_the_fetch_withdraws_the_answer(server: Server) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(server))
    server.directory.remove_group_member(server.builder, "project-1", "finance", ana)
    assert code(http.get(JOBS + job)) == (401, "SESSION_EXPIRED")  # the change ended it
    withdrawn = fetched(server.client("ana"), job)  # signed in again
    assert code(withdrawn) == (409, "QUERY_RESULT_WITHDRAWN")
    assert "rows" not in withdrawn.text


@pytest.mark.parametrize("change", ["role", "membership"])
def test_a_role_lowered_or_a_membership_removed_before_the_fetch_is_forbidden(
    server: Server, change: str
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(server))
    if change == "role":
        server.directory.change_role(server.builder, "project-1", ana, "viewer")
    else:
        server.directory.remove_membership(server.builder, "project-1", ana)
    refused = http.get(JOBS + job)
    assert code(refused) == (401, "SESSION_EXPIRED")
    again = server.client("ana").get(JOBS + job, headers={"Accept": semantic_http.ARROW})
    assert code(again) == (403, "ACCESS_FORBIDDEN")
    assert "rows" not in again.text
    assert ("delivered", job) not in stages(server)


# --- AC3b: the slot of a job ---------------------------------------------------------------------


class Gate:
    """Holds every question's execution, once its engine is known, until opened."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.entered = threading.Event()  # an execution is held


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    held = Gate()
    original = answer

    def holding(*args: Any) -> Any:  # noqa: ANN401
        held.entered.set()
        assert held.opened.wait(30)
        return original(*args)

    monkeypatch.setattr(semantic_http, "answer", holding)
    return held


def test_a_running_job_holds_its_slot_and_a_cancel_frees_it_at_once(
    server: Server, gate: Gate
) -> None:
    ana = server.author("ana", "finance")
    QueryLimits(server.store, server.audit).change(server.admin, {"author": RoleLimit(1, 60_000)})
    http = server.client("ana")
    job = start(http, detail(10))
    assert gate.entered.wait(30)  # running, its engine known: the managed lake
    assert http.get(JOBS + job).json()["status"] == "running"
    refused = http.post("/api/v1/semantic/query", json=detail(10), headers=ASYNC)
    assert code(refused) == (429, "QUERY_BUDGET_EXCEEDED")  # and no job exists for it
    assert code(ask(http, detail(1))) == (429, "QUERY_BUDGET_EXCEEDED")
    cancelled = http.delete(JOBS + job, headers=SAME)
    assert cancelled.json()["status"] == "cancelled"
    assert server.semantic.slots.running(ana, "project-1") == 0  # free before any fetch
    second = start(http, detail(10))  # at the limit again, at once
    gate.opened.set()
    assert fetched(http, second).status_code == 200
    assert http.get(JOBS + job).json()["status"] == "cancelled"  # never delivered
    assert ("delivered", job) not in stages(server)
    until(lambda: server.semantic.slots.running(ana, "project-1") == 0)
    assert ended(server) == [(job, "cancelled")]  # its one executed event, never a second
    assert stages(server) == [("executed", second), ("delivered", second)]


def test_a_job_that_fails_frees_its_slot(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")

    def broken(*args: Any) -> Any:  # noqa: ANN401
        raise semantic_http.ProjectBusy

    monkeypatch.setattr(semantic_http, "answer", broken)
    job = start(http, detail(10))
    assert code(fetched(http, job)) == (503, "PROJECT_BUSY")
    until(lambda: server.semantic.slots.running(ana, "project-1") == 0)
    until(lambda: ended(server) == [(job, "failed")])  # its one executed event
    assert stages(server) == []  # no answer, no receipt


def test_a_revocation_after_a_fetch_applies_to_the_next_fetch(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    until(lambda: ("executed", job) in stages(server))
    recording = record_query
    revoked: list[bool] = []

    def recorded_then_revoked(*args: Any) -> None:  # noqa: ANN401
        recording(*args)
        if args[3] == "delivered" and not revoked:
            revoked.append(True)
            server.attributes.set(server.builder, "project-1", ana, "tariff", "text", "x")

    monkeypatch.setattr(semantic_delivery, "record_query", recorded_then_revoked)
    assert fetched(http, job).status_code == 200  # decided before the revocation
    assert code(fetched(http, job)) == (409, "QUERY_RESULT_WITHDRAWN")


def test_cancelling_an_uninterruptible_job_and_starting_again_never_exceeds_the_limit(
    server: Server, gate: Gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As an existing lake's or ClickHouse's question: the result is withdrawn at the cancel,
    the slot held until the execution really ends; repeated cancels and starts find no room."""
    learnt = Cancel.known

    def uninterruptible(self: Cancel, interruptible: bool) -> None:
        learnt(self, False)

    monkeypatch.setattr(Cancel, "known", uninterruptible)
    ana = server.author("ana", "finance")
    QueryLimits(server.store, server.audit).change(server.admin, {"author": RoleLimit(1, 60_000)})
    http = server.client("ana")
    job = start(http, detail(10))
    assert gate.entered.wait(30)
    assert http.delete(JOBS + job, headers=SAME).json()["status"] == "cancelled"
    for _ in range(3):
        again = http.post("/api/v1/semantic/query", json=detail(10), headers=ASYNC)
        assert code(again) == (429, "QUERY_BUDGET_EXCEEDED")
        assert code(ask(http, detail(1))) == (429, "QUERY_BUDGET_EXCEEDED")
        assert http.delete(JOBS + job, headers=SAME).json()["status"] == "cancelled"
        assert server.semantic.slots.running(ana, "project-1") == 1
    assert http.get(JOBS + job).json() == {"job": job, "status": "cancelled"}  # withdrawn
    gate.opened.set()
    until(lambda: server.semantic.slots.running(ana, "project-1") == 0)
    assert ended(server) == [(job, "cancelled")]
    assert fetched(http, start(http, detail(10))).status_code == 200  # room again


def test_an_answer_beyond_the_retention_is_an_explicit_failure_not_a_404(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_jobs, "MAX_BYTES", 1)
    server.author("ana", "finance")
    http = server.client("ana")
    job = start(http, detail(10))
    failed = fetched(http, job)
    assert code(failed) == (413, "QUERY_BUDGET_EXCEEDED")
    assert failed.json()["error"]["semantic"]["details"]["budget"] == "retained_bytes"
    assert "rows" not in failed.text
    assert ended(server) == [(job, "failed")]
    assert code(http.get(JOBS + job)) == (413, "QUERY_BUDGET_EXCEEDED")  # kept, verifiable
    assert ask(http, detail(10)).status_code == 200  # whole, synchronously
