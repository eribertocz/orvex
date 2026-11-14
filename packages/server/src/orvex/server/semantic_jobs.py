"""Asynchronous semantic queries (#445; Q1 = A): asked with `Prefer: respond-async`, run in the
background under the caller's role limits, and delivered only through the one delivery step.

- **Start.** The caller's slot is taken before the job exists: a caller at their limit gets
  `429` and no job. The job id is 128 random bits, owned by the caller's user and project; any
  other caller gets `404` for it, as for an id that never existed.
- **The slot** is released exactly once, at the first of: the job finishing, failing or being
  cancelled (Owner precision 2), never at the fetch; a fetch takes no slot. A cancel also stops
  the execution (`query_cancel`): its managed-lake read is interrupted, and its answer, if one
  still comes, is never delivered.
- **Audit at execution** (precision 3). An execution that completes records its `query` event
  (`stage: executed`, with its id) and #443's events at once, fetched or not, even when the
  job was cancelled meanwhile: its rows were read. When that event cannot be recorded the job
  fails with `AUDIT_UNAVAILABLE` and its result is discarded, unless the answer is withheld
  (#443). An execution that fails has no answer, so no receipt and no `query` event, as a
  refused synchronous question has none.
- **Fetch.** A finished job's answer goes through `semantic_delivery.deliver` (`stage:
  delivered`), with a context built for the fetch: permissions changed since the job ran give
  `QUERY_RESULT_WITHDRAWN`, and nothing of the answer is sent, in whole or in part.
- **Keeping.** A finished job is kept for 10 minutes, and at most `MAX_JOBS` per process; then
  it is gone (`404`). A restart loses every job (fail closed). The answer is never truncated:
  it is the whole answer, or its error.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from orvex.server import semantic_http
from orvex.server.query_cancel import Cancel
from orvex.server.query_slots import Slot
from orvex.server.semantic_audit import record_query, record_withheld
from orvex.server.semantic_delivery import deliver

KEPT_SECONDS = 600
MAX_JOBS = 256


class JobNotFound(Exception):
    """No job with this id for this caller: unknown, expired or another caller's."""


@dataclass
class Job:
    owner: tuple[str, str]  # user, project
    request: object
    slot: Slot
    stop: Cancel = field(default_factory=Cancel)
    status: str = "running"  # running, done, failed, cancelled
    asked: semantic_http.Asked | None = None
    error: Exception | None = None
    ended: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Jobs:
    """One process's asynchronous jobs."""

    def __init__(
        self,
        semantic: semantic_http.Semantic,
        clock: Callable[[], float] = time.monotonic,
        run: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._semantic = semantic
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._run = run or (lambda work: threading.Thread(target=work, daemon=True).start())

    def start(self, body: bytes, request: object, owner: tuple[str, str]) -> str:
        """Start a job for `request`; the caller's slot is taken now, or this refuses and no
        job exists."""
        semantic = self._semantic
        slot = semantic_http.take_slot(semantic, request)
        job_id = secrets.token_hex(16)
        job = Job(owner, request, slot)

        def work() -> None:
            try:
                asked = semantic_http.ask(semantic, body, request, job.stop)
                _executed(semantic, asked, job_id)  # audited at execution, fetched or not
                self._end(job, "done", asked=asked)
            except Exception as error:  # noqa: BLE001 - kept as the job's outcome
                self._end(job, "failed", error=error)

        self._collect()
        with self._lock:
            self._jobs[job_id] = job
        try:
            self._run(work)
        except BaseException:
            with self._lock:
                del self._jobs[job_id]
            slot.release()
            raise
        return job_id

    def status(self, job_id: str, owner: tuple[str, str]) -> Job:
        self._collect()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise JobNotFound(job_id)
        return job

    def cancel(self, job_id: str, owner: tuple[str, str]) -> Job:
        job = self.status(job_id, owner)
        self._end(job, "cancelled")
        job.stop.cancel()
        return job

    def fetch(
        self,
        job_id: str,
        owner: tuple[str, str],
        request: object,
        revalidate: Callable[[], None] | None = None,
    ) -> semantic_http.Asked | None:
        """A finished job's answer, delivered now (None while it runs); its error raised."""
        job = self.status(job_id, owner)
        with job.lock:
            status, asked, error = job.status, job.asked, job.error
        if status == "running" or status == "cancelled":
            return None
        if status == "failed" and error is not None:
            raise error
        assert asked is not None
        deliver(self._semantic, request, asked, "delivered", revalidate, job_id)
        return asked

    def _end(
        self,
        job: Job,
        status: str,
        asked: semantic_http.Asked | None = None,
        error: Exception | None = None,
    ) -> None:
        """The job's first end wins, and releases its slot, exactly once: a cancel is never
        overwritten by a later finish."""
        with job.lock:
            if job.status != "running":
                return
            job.status, job.asked, job.error, job.ended = status, asked, error, self._clock()
        job.slot.release()

    def _collect(self) -> None:
        """Forget jobs kept past their time, and the oldest beyond the bound."""
        now = self._clock()
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.ended is not None and now - job.ended >= KEPT_SECONDS:
                    del self._jobs[job_id]
            ended = sorted((j.ended, i) for i, j in self._jobs.items() if j.ended is not None)
            while len(self._jobs) >= MAX_JOBS and ended:
                del self._jobs[ended.pop(0)[1]]


def _executed(semantic: semantic_http.Semantic, asked: semantic_http.Asked, job: str) -> None:
    """The job's execution events (`stage: executed`): with no revalidation, since nothing
    leaves; `AUDIT_UNAVAILABLE` fails the job unless the answer is withheld."""
    audit = semantic.audit
    if audit is None:
        return
    record_query(audit, asked.found, asked.context, "executed", job)
    record_withheld(audit, asked.found, asked.context)
