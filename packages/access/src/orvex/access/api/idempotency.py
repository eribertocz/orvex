"""Idempotency of the operations that change state (#486 G3): one idempotency key, one effect.

A call is recorded under its operation, its actor and its idempotency key, with a digest of its
arguments. Repeated with the same key and the same arguments, it is not run again: it answers
what the first call answered, a refusal included. The same key with other arguments is refused
(`VALIDATION_FAILED`), never run as a second call. Calls with one key run one at a time, so two
concurrent repeats still have one effect. The record is this implementation's memory, and keeps
the most recent `CAPACITY` calls; it holds only what the first call returned.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from orvex.contracts.access.errors import VALIDATION_FAILED, AccessError
from orvex.contracts.envelope import CallEnvelope

CAPACITY = 4096

type Key = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _Done:
    digest: str
    result: object
    refusal: AccessError | None


def _digest(arguments: tuple[object, ...]) -> str:
    return hashlib.sha256(repr(arguments).encode()).hexdigest()


class Replays:
    """The record of the calls that changed state, by operation, actor and key."""

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._capacity = capacity
        self._done: OrderedDict[Key, _Done] = OrderedDict()
        self._running: dict[Key, threading.Lock] = {}
        self._lock = threading.Lock()

    def once[T](
        self,
        operation: str,
        envelope: CallEnvelope,
        arguments: tuple[object, ...],
        work: Callable[[], T],
    ) -> T:
        """`work()` the first time this key is used; its answer again on every repeat."""
        key = (operation, envelope.actor, envelope.idempotency_key or "")
        digest = _digest(arguments)
        with self._lock:
            running = self._running.setdefault(key, threading.Lock())
        with running:
            done = self._recorded(key)
            if done is None:
                try:
                    done = self._run(key, digest, work)
                finally:
                    with self._lock:
                        self._running.pop(key, None)
            if done.digest != digest:
                raise AccessError(
                    VALIDATION_FAILED,
                    "The idempotency key was used for another call.",
                    {"field": "idempotency_key"},
                )
            if done.refusal is not None:
                raise done.refusal
            return done.result  # type: ignore[return-value]

    def _recorded(self, key: Key) -> _Done | None:
        with self._lock:
            done = self._done.get(key)
            if done is not None:
                self._done.move_to_end(key)
            return done

    def _run[T](self, key: Key, digest: str, work: Callable[[], T]) -> _Done:
        try:
            done = _Done(digest, work(), None)
        except AccessError as refusal:
            done = _Done(digest, None, refusal)
        with self._lock:
            self._done[key] = done
            while len(self._done) > self._capacity:
                self._done.popitem(last=False)
        return done
