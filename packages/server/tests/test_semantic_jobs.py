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


class Table:
    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes


@dataclass
class Asked:
    """What a job keeps of an answer: its result's Arrow size."""

    nbytes: int = 100

    @property
    def found(self) -> Any:  # noqa: ANN401
        size = self.nbytes

        class Result:
            def arrow(self) -> Table:
                return Table(size)

        class Found:
            result = Result()

        return Found()


def answered_with(
    monkeypatch: pytest.MonkeyPatch, outcome: Callable[[], Any], interruptible: bool = True
) -> None:
    """`ask` as the engine would: it learns its engine (`known`), then answers."""

    def ask(semantic: Any, body: bytes, request: object, cancel: Cancel) -> Any:  # noqa: ANN401
        cancel.known(interruptible)
        return outcome()

    monkeypatch.setattr(semantic_http, "ask", ask)
    monkeypatch.setattr(semantic_jobs, "deliver", lambda *args: None)


def now(work: Callable[[], None]) -> None:
    work()


def test_a_finished_job_frees_its_slot_once(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, Asked)
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
        jobs.fetch(job, OWNER, object(), lambda asked: asked)


def test_a_start_at_the_limit_is_refused_and_leaves_no_job(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, Asked)
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


def running_job(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch, interruptible: bool
) -> tuple[Jobs, str, threading.Event, threading.Thread]:
    """A job whose execution has learnt its engine and is still running, until the event."""
    release, reading = threading.Event(), threading.Event()

    def answer() -> Any:  # noqa: ANN401
        reading.set()
        assert release.wait(10)
        return Asked()

    answered_with(monkeypatch, answer, interruptible)
    threads: list[threading.Thread] = []
    jobs = jobs_of(stub, clock, lambda work: threads.append(threading.Thread(target=work)))
    job = jobs.start(b"{}", object(), OWNER)
    threads[0].start()
    assert reading.wait(10)
    return jobs, job, release, threads[0]


def test_a_cancel_of_an_interruptible_execution_frees_the_slot_at_once(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, job, release, thread = running_job(stub, clock, monkeypatch, interruptible=True)
    assert jobs.cancel(job, OWNER).status == "cancelled"
    assert jobs.status(job, OWNER).stop.cancelled  # its managed reads interrupted
    assert stub.slots.running(*OWNER) == 0
    release.set()
    thread.join(10)
    assert stub.slots.running(*OWNER) == 0  # never released twice
    assert jobs.status(job, OWNER).status == "cancelled"
    assert jobs.fetch(job, OWNER, object(), lambda asked: asked) is None  # never delivered


def test_a_cancel_of_an_execution_that_cannot_be_interrupted_holds_the_slot_until_it_ends(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing lake's or ClickHouse's read runs on after the cancel: the result is
    withdrawn at once, the slot only when the execution really ends."""
    jobs, job, release, thread = running_job(stub, clock, monkeypatch, interruptible=False)
    assert jobs.cancel(job, OWNER).status == "cancelled"
    assert jobs.fetch(job, OWNER, object(), lambda asked: asked) is None  # withdrawn now
    assert stub.slots.running(*OWNER) == 1  # still running: still counted
    release.set()
    thread.join(10)
    assert stub.slots.running(*OWNER) == 0
    assert jobs.status(job, OWNER).status == "cancelled"
    assert jobs.fetch(job, OWNER, object(), lambda asked: asked) is None


def test_a_job_cancelled_before_it_knows_its_engine_holds_its_slot_and_stops_before_reading(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[bool] = []

    def reading() -> Any:  # noqa: ANN401
        read.append(True)
        return Asked()

    answered_with(monkeypatch, reading)
    held: list[Callable[[], None]] = []
    jobs = jobs_of(stub, clock, held.append)
    job = jobs.start(b"{}", object(), OWNER)  # waiting for its turn, or planning
    jobs.cancel(job, OWNER)
    assert stub.slots.running(*OWNER) == 1  # not known to be interruptible: held
    held[0]()  # its engine becomes known: it stops there
    assert read == []
    assert stub.slots.running(*OWNER) == 0
    assert jobs.status(job, OWNER).status == "cancelled"


def test_cancelling_and_starting_again_never_exceeds_the_limit(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executions that cannot be interrupted: however many cancels and starts, at most
    `max_concurrent` run, and each slot comes back once its execution ends."""
    stub.limit = 2
    release = threading.Event()
    running = threading.Semaphore(0)

    def answer() -> Any:  # noqa: ANN401
        running.release()
        assert release.wait(10)
        return Asked()

    answered_with(monkeypatch, answer, interruptible=False)
    threads: list[threading.Thread] = []

    def run(work: Callable[[], None]) -> None:
        thread = threading.Thread(target=work)
        threads.append(thread)
        thread.start()

    jobs = jobs_of(stub, clock, run)
    for _ in range(2):
        job = jobs.start(b"{}", object(), OWNER)
        assert running.acquire(timeout=10)
        jobs.cancel(job, OWNER)
    for _ in range(5):  # cancelled, but still running: no room
        with pytest.raises(SemanticError) as caught:
            jobs.start(b"{}", object(), OWNER)
        assert caught.value.details["budget"] == "concurrent_queries"
        assert stub.slots.running(*OWNER) == 2
    release.set()
    for thread in threads:
        thread.join(10)
    assert stub.slots.running(*OWNER) == 0
    again = jobs.start(b"{}", object(), OWNER)  # room again
    threads[-1].join(10)
    assert jobs.status(again, OWNER).status == "done"
    assert stub.slots.running(*OWNER) == 0


def test_an_answer_larger_than_the_retention_fails_explicitly_and_is_never_kept(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered_with(monkeypatch, lambda: Asked(semantic_jobs.MAX_BYTES + 1))
    log = Recorded()
    stub.audit = log  # type: ignore[assignment]
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    found = jobs.status(job, OWNER)
    assert (found.status, found.asked, found.size) == ("failed", None, 0)
    assert isinstance(found.error, SemanticError)
    assert found.error.code == "QUERY_BUDGET_EXCEEDED"
    assert found.error.details == {
        "budget": "retained_bytes",
        "limit": str(semantic_jobs.MAX_BYTES),
        "estimate": str(semantic_jobs.MAX_BYTES + 1),
    }
    with pytest.raises(SemanticError):
        jobs.fetch(job, OWNER, object(), lambda asked: asked)
    assert [e["details"] for e in log.events] == [
        {"stage": "executed", "job": job, "end": "failed"}
    ]
    assert stub.slots.running(*OWNER) == 0
    stub.audit = None
    answered_with(monkeypatch, lambda: Asked(semantic_jobs.MAX_BYTES))  # at the bound: kept
    assert jobs.status(jobs.start(b"{}", object(), OWNER), OWNER).status == "done"


def test_racing_ends_release_the_slot_exactly_once(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many threads end the same jobs at once, by finish and by cancel: each job has one
    outcome and its slot counts down once; the count never goes below zero."""
    stub.limit = 64
    gate = threading.Barrier(9)

    def answer() -> Any:  # noqa: ANN401
        gate.wait(10)
        return Asked()

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
    answered_with(monkeypatch, Asked)
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
    answered_with(monkeypatch, Asked)
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
    answered_with(monkeypatch, Asked)
    jobs = jobs_of(stub, clock, now)
    kept = []
    for second in range(MAX_JOBS + 3):
        clock.now = second / 10
        kept.append(jobs.start(b"{}", object(), OWNER))
    assert len(jobs._jobs) <= MAX_JOBS
    with pytest.raises(JobNotFound):
        jobs.status(kept[0], OWNER)
    assert jobs.status(kept[-1], OWNER).status == "done"


def test_the_kept_answers_are_bounded_in_bytes_and_the_heaviest_owner_goes_first(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One owner filling the byte bound evicts their own oldest answers, never another's."""
    sizes = iter([10, *([semantic_jobs.MAX_BYTES // 4] * 6)])
    answered_with(monkeypatch, lambda: Asked(next(sizes)))
    jobs = jobs_of(stub, clock, now)
    other = ("ben", "project-1")
    small = jobs.start(b"{}", object(), other)
    heavy = []
    for second in range(6):
        clock.now = second + 1
        heavy.append(jobs.start(b"{}", object(), OWNER))
    jobs.status(heavy[-1], OWNER)  # collects
    assert jobs.status(small, other).status == "done"  # the other owner's answer stays
    assert jobs._kept_bytes() <= semantic_jobs.MAX_BYTES + semantic_jobs.MAX_BYTES // 4
    with pytest.raises(JobNotFound):
        jobs.status(heavy[0], OWNER)
    assert jobs.status(heavy[-1], OWNER).status == "done"


@dataclass
class Recorded:
    """An audit log that keeps the `query` events it is given."""

    events: list[dict[str, Any]] = field(default_factory=list)
    unrecorded: int = 0

    def record_alone(self, kind: str, actor: str, details: Any, **more: Any) -> str:  # noqa: ANN401
        self.events.append({"kind": kind, "actor": actor, "details": dict(details), **more})
        return "id"


@pytest.mark.parametrize("end", ["failed", "cancelled"])
def test_a_job_ended_with_no_answer_records_one_executed_event_with_its_end(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    def failing() -> Any:  # noqa: ANN401
        raise SemanticError("MEMBER_NOT_FOUND", {"member": "x"})

    answered_with(monkeypatch, failing if end == "failed" else Asked)
    log = Recorded()
    stub.audit = log  # type: ignore[assignment]
    held: list[Callable[[], None]] = []
    jobs = jobs_of(stub, clock, held.append)
    job = jobs.start(b"{}", object(), OWNER)
    if end == "cancelled":
        jobs.cancel(job, OWNER)
        jobs.cancel(job, OWNER)  # a second cancel records nothing more
    held[0]()  # the execution ends after the cancel: nothing more either
    assert log.events == [
        {
            "kind": "query",
            "actor": "ana",
            "details": {"stage": "executed", "job": job, "end": end},
            "outcome": "allowed",
            "project": "project-1",
        }
    ]
    assert stub.slots.running(*OWNER) == 0


def test_an_unexpected_failure_at_the_end_still_ends_the_job_and_frees_its_slot(
    stub: Stub, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(Asked):
        @property
        def found(self) -> Any:  # noqa: ANN401
            raise RuntimeError("the answer cannot be measured")

    answered_with(monkeypatch, Broken)
    jobs = jobs_of(stub, clock, now)
    job = jobs.start(b"{}", object(), OWNER)
    found = jobs.status(job, OWNER)
    assert found.status == "failed"
    assert isinstance(found.error, RuntimeError)
    assert stub.slots.running(*OWNER) == 0
