"""Each caller's running questions, against their role's limit (#445; #449 Q8, Q3 = A): one
slot per question running, per user and project, in this process.

- A question beyond the limit is refused before any planning, with
  `QUERY_BUDGET_EXCEEDED` (`budget: concurrent_queries`), never queued.
- A slot is released **exactly once**, whatever ends it first: a synchronous question's
  delivery, or an asynchronous job's finish, failure or cancel (Owner precision 2). A second
  release, from a race, finds it released and changes nothing.
"""

from __future__ import annotations

import threading
from collections import Counter

from orvex.semantic.errors import SemanticError


class Slot:
    """One running question's slot; `release` is idempotent and thread-safe."""

    def __init__(self, slots: QuerySlots, key: tuple[str, str]) -> None:
        self._slots = slots
        self._key = key
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> bool:
        """Release the slot; False when it was already released."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._slots._free(self._key)
        return True


class QuerySlots:
    """The process's slots, by user and project."""

    def __init__(self) -> None:
        self._running: Counter[tuple[str, str]] = Counter()
        self._lock = threading.Lock()

    def take(self, user: str, project: str, limit: int) -> Slot:
        key = (user, project)
        with self._lock:
            if self._running[key] >= limit:
                raise SemanticError(
                    "QUERY_BUDGET_EXCEEDED", {"budget": "concurrent_queries", "limit": str(limit)}
                )
            self._running[key] += 1
        return Slot(self, key)

    def running(self, user: str, project: str) -> int:
        with self._lock:
            return self._running[(user, project)]

    def _free(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._running[key] -= 1
            if self._running[key] <= 0:
                del self._running[key]
