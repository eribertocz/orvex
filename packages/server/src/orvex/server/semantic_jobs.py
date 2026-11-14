"""Asynchronous semantic queries (#445; Q1 = A): asked with `Prefer: respond-async`, run in the
background under the caller's role limits, and delivered only through the one delivery step.

- **Start.** The caller's slot is taken before the job exists: a caller at their limit gets
  `429` and no job. The job id is 128 random bits, owned by the caller's user and project; any
  other caller gets `404` for it, as for an id that never existed.
- **One end.** A job ends once, at the first of: finishing, failing or being cancelled. That
  end releases the slot exactly once (Owner precision 2), never the fetch, which takes no slot,
  and records the job's one `executed` event (precision 3), fetched or not: with the receipt
  when it finished, with its `end` (failed, cancelled) and no receipt otherwise. A cancel also
  stops the execution (`query_cancel`); an answer that still comes is never delivered.
- **Audit at execution.** When a finished job's `executed` event cannot be recorded, the job
  fails with `AUDIT_UNAVAILABLE` and its result is discarded, unless the answer is withheld
  (#443).
- **Fetch.** A finished job's answer goes through `semantic_delivery.deliver` (`stage:
  delivered`), with a context built for the fetch, after its response is built whole:
  permissions changed since the job ran give `QUERY_RESULT_WITHDRAWN`, and nothing of the
  answer is sent, in whole or in part.
- **Keeping.** A finished job is kept for 10 minutes, then it is gone (`404`). The kept answers
  are bounded, like the result cache, in number (`MAX_JOBS`) and in bytes (`MAX_BYTES`): beyond
  either, the oldest finished job of the owner keeping the most bytes goes first, so a caller
  filling the bound mostly evicts their own. A restart loses every job (fail closed). The answer
  is never truncated: it is the whole answer, or its error.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from orvex.access.errors import AccessError
from orvex.server import semantic_http
from orvex.server.query_cancel import Cancel
from orvex.server.query_slots import Slot
from orvex.server.semantic_audit import record_query, record_unfinished, record_withheld
from orvex.server.semantic_delivery import deliver

KEPT_SECONDS = 600
MAX_JOBS = 256
MAX_BYTES = 256 * 2**20  # the kept answers' tables at most, as the result cache's bound


class JobNotFound(Exception):
    """No job with this id for this caller: unknown, expired or another caller's."""


@dataclass
class Job:
    owner: tuple[str, str]  # user, project
    request: object
    slot: Slot
    stop: Cancel = field(default_factory=Cancel)
    status: str = "running"  # running, done, failed, cancelled
    claimed: bool = False  # its one end has begun
    asked: semantic_http.Asked | None = None
    error: Exception | None = None
    ended: float | None = None
    size: int = 0
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
            except Exception as error:  # noqa: BLE001 - kept as the job's outcome
                self._end(job, job_id, "failed", error=error)
                return
            self._end(job, job_id, "done", asked=asked)

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
        if self._end(job, job_id, "cancelled"):
            job.stop.cancel()
        return job

    def fetch[R](
        self,
        job_id: str,
        owner: tuple[str, str],
        request: object,
        respond: Callable[[semantic_http.Asked], R],
        revalidate: Callable[[], None] | None = None,
    ) -> R | None:
        """A finished job's response, built whole, then delivered now (None while it runs or
        once cancelled); its error raised."""
        job = self.status(job_id, owner)
        with job.lock:
            status, asked, error = job.status, job.asked, job.error
        if status in {"running", "cancelled"}:
            return None
        if status == "failed" and error is not None:
            raise error
        assert asked is not None
        response = respond(asked)  # the whole response, before anything is decided
        deliver(self._semantic, request, asked, "delivered", revalidate, job_id)
        return response

    def _end(
        self,
        job: Job,
        job_id: str,
        status: str,
        asked: semantic_http.Asked | None = None,
        error: Exception | None = None,
    ) -> bool:
        """The job's one end, whichever comes first: its `executed` event, then its outcome,
        then its slot released. False when another end came first (a cancel racing the
        finish): nothing more is recorded or released."""
        with job.lock:
            if job.claimed:
                return False
            job.claimed = True
        user, project = job.owner
        audit = self._semantic.audit
        size = 0
        try:
            if status == "done" and asked is not None:
                if audit is not None:
                    record_query(audit, asked.found, asked.context, "executed", job_id)
                    record_withheld(audit, asked.found, asked.context)
                size = asked.found.result.arrow().nbytes
            elif audit is not None:
                record_unfinished(audit, user, project, job_id, status)
        except AccessError as refused:  # the event of an answer could not be recorded
            status, asked, error = "failed", None, refused
        with job.lock:
            job.status, job.asked, job.error = status, asked, error
            job.ended, job.size = self._clock(), size
        job.slot.release()
        return True

    def _collect(self) -> None:
        """Forget the jobs kept past their time, then the oldest finished of the owner keeping
        the most bytes while the bounds are passed."""
        now = self._clock()
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.ended is not None and now - job.ended >= KEPT_SECONDS:
                    del self._jobs[job_id]
            while len(self._jobs) >= MAX_JOBS or self._kept_bytes() > MAX_BYTES:
                held: Counter[tuple[str, str]] = Counter()
                for job in self._jobs.values():
                    if job.ended is not None:
                        held[job.owner] += job.size + 1
                if not held:
                    return
                heaviest = held.most_common(1)[0][0]
                ended = [
                    (j.ended or 0.0, i)
                    for i, j in self._jobs.items()
                    if j.ended is not None and j.owner == heaviest
                ]
                del self._jobs[min(ended)[1]]

    def _kept_bytes(self) -> int:
        return sum(job.size for job in self._jobs.values())
