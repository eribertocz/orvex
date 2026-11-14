"""#445 AC3b and AC4 on the job table itself: a slot is released exactly once whatever races to
end it (finish, failure, cancel, from many threads); a refused start leaves no job; a finished
job is kept ten minutes and at most `MAX_JOBS`; a cancel is never overwritten by a later
finish."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from orvex.access.authentication.query_limits import RoleLimit
from orvex.semantic.errors import SemanticError
from orvex.server import semantic_http, semantic_jobs
from orvex.server.query_cancel import Cancel
from orvex.server.query_slots import QuerySlots
from orvex.server.semantic_jobs import KEPT_SECONDS, MAX_JOBS, JobNotFound, Jobs

OWNER = ("ana", "project-1")


@dataclass
class Stub:
    """What `Jobs` reads of `Semantic`: the caller's identity, the limits, the slots."""

    limit: int = 2
    slots: QuerySlots = field(default_factory=QuerySlots)
    audit: None = None

    def identity(self, request: object) -> tuple[str, str, str]:
        return "ana", "project-1", "author"

    def limits(self, role: str) -> RoleLimit:
        return RoleLimit(self.limit, 60_000)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def stub() -> Stub:
    return Stub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def jobs_of(stub: Stub, clock: Clock, run: Callable[[Callable[[], None]], None]) -> Jobs:
    return Jobs(stub, clock, run)  # type: ignore[arg-type]


def answered_with(monkeypatch: pytest.MonkeyPatch, outcome: Callable[[], Any]) -> None:
    def ask(semantic: Any, body: bytes, request: object, cancel: Cancel) -> Any:  # noqa: ANN401
        return outcome()

    monkeypatch.setattr(semantic_http, "ask", ask)
    monkeypatch.setattr(semantic_jobs, "deliver", lambda *args: None)


def now(work: Callable[[], None]) -> None:
    work()


def test_a_finished_job_frees_its_slot_once(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: "the answer")
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    assert jobs.status(job, OWNER).status == "done"
    assert stub.slots.running(*OWNER) == 0
    assert jobs.status(job, OWNER).slot.release() is False  # already released, once


def test_a_failed_job_frees_its_slot_and_keeps_its_error(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing() -> Any:  # noqa: ANN401
        raise SemanticError("MEMBER_NOT_FOUND", {"member": "x"})

    answered_with(monkeypatch, failing)
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    found = jobs.status(job, OWNER)
    assert found.status == "failed"
    assert isinstance(found.error, SemanticError)
    assert stub.slots.running(*OWNER) == 0
    with pytest.raises(SemanticError):
        jobs.fetch(job, OWNER, object())


def test_a_start_at_the_limit_is_refused_and_leaves_no_job(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: "the answer")
    held: list[Callable[[], None]] = []
    jobs = jobs_of(stub, clock, held.append)  # started, never run
    first, second = jobs.start(b"{}", object(), OWNER), jobs.start(b"{}", object(), OWNER)
    with pytest.raises(SemanticError) as caught:
        jobs.start(b"{}", object(), OWNER)
    assert caught.value.details == {"budget": "concurrent_queries", "limit": "2"}
    assert len(jobs._jobs) == 2
    assert stub.slots.running(*OWNER) == 2
    for work in held:
        work()
    assert {jobs.status(j, OWNER).status for j in (first, second)} == {"done"}
    assert stub.slots.running(*OWNER) == 0


def test_a_job_that_cannot_be_run_frees_its_slot(stub: Stub, clock: Clock) -> None:
    def broken(work: Callable[[], None]) -> None:
        raise RuntimeError("no thread")

    jobs = jobs_of(stub, clock, broken)
    with pytest.raises(RuntimeError):
        jobs.start(b"{}", object(), OWNER)
    assert stub.slots.running(*OWNER) == 0
    assert jobs._jobs == {}


def test_a_cancel_frees_the_slot_at_once_stops_the_execution_and_is_never_overwritten(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    stops: list[Cancel] = []

    def ask(semantic: Any, body: bytes, request: object, cancel: Cancel) -> Any:  # noqa: ANN401
        stops.append(cancel)
        return "the answer"

    monkeypatch.setattr(semantic_http, "ask", ask)
    held: list[Callable[[], None]] = []
    jobs = jobs_of(stub, clock, held.append)
    job = jobs.start(b"{}", object(), OWNER)
    assert jobs.cancel(job, OWNER).status == "cancelled"
    assert stub.slots.running(*OWNER) == 0  # free before the execution even ran
    assert jobs.status(job, OWNER).stop.cancelled
    held[0]()  # the execution comes, under its stopped cancel, and finishes anyway
    assert stops == [jobs.status(job, OWNER).stop]
    assert stub.slots.running(*OWNER) == 0  # never released twice
    assert jobs.status(job, OWNER).status == "cancelled"
    assert jobs.fetch(job, OWNER, object()) is None  # never delivered


def test_racing_ends_release_the_slot_exactly_once(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many threads end the same jobs at once, by finish and by cancel: each job has one
    outcome and its slot counts down once; the count never goes below zero."""
    stub.limit = 64
    gate = threading.Barrier(9)

    def answer() -> Any:  # noqa: ANN401
        gate.wait(10)
        return "the answer"

    answered_with(monkeypatch, answer)
    threads: list[threading.Thread] = []

    def run(work: Callable[[], None]) -> None:
        threads.append(threading.Thread(target=work))

    jobs = jobs_of(stub, clock, run)
    started = [jobs.start(b"{}", object(), OWNER) for _ in range(8)]
    assert stub.slots.running(*OWNER) == 8
    cancels = [
        threading.Thread(target=jobs.cancel, args=(j, OWNER)) for j in started for _ in range(2)
    ]  # a cancel racing the finish, and two cancels at once
    for thread in threads + cancels:
        thread.start()
    gate.wait(10)
    for thread in threads + cancels:
        thread.join(10)
    assert stub.slots.running(*OWNER) == 0
    for job in started:
        found = jobs.status(job, OWNER)
        assert found.status in {"done", "cancelled"}
        assert found.slot.release() is False


def test_another_owner_never_finds_the_job(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: "the answer")
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    for other in (("ben", "project-1"), ("ana", "project-2")):
        with pytest.raises(JobNotFound):
            jobs.status(job, other)
        with pytest.raises(JobNotFound):
            jobs.cancel(job, other)
    assert jobs.status(job, OWNER).status == "done"


def test_a_finished_job_is_kept_ten_minutes(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: "the answer")
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    clock.now = KEPT_SECONDS - 1
    assert jobs.status(job, OWNER).status == "done"
    clock.now = KEPT_SECONDS
    with pytest.raises(JobNotFound):
        jobs.status(job, OWNER)


def test_at_most_max_jobs_are_kept_the_oldest_finished_go_first(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: "the answer")
    jobs = jobs_of(stub, clock, now)
    kept = []
    for second in range(MAX_JOBS + 3):
        clock.now = second / 10
        kept.append(jobs.start(b"{}", object(), OWNER))
    assert len(jobs._jobs) <= MAX_JOBS
    with pytest.raises(JobNotFound):
        jobs.status(kept[0], OWNER)
    assert jobs.status(kept[-1], OWNER).status == "done"
