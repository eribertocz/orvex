"""What the store's contract implementation holds open for its callers, by handle id, and the
outcome of each state-changing call by idempotency key, so a repeated key has its effect once.

Every id is random (`secrets.token_hex`) and never reused; an id that names nothing held is
refused with STORE_PARAMETER_INVALID. The outcomes of calls on a handle are forgotten when the
handle is let go; the outcomes of calls in the store's own scope (those that open or end a
handle) are kept for the last `KEPT` such calls. What is remembered never outgrows that.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from orvex.contracts.store.errors import StoreError

STORE_SCOPE = "store"  # the scope of a call that opens something: no handle exists yet
KEPT = 4096


@dataclass(slots=True)
class Entry:
    kind: str
    value: object
    owner: str | None = None  # the handle this one was opened on, if any
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class _Outcome:
    value: object = None
    error: BaseException | None = None


class Holdings:
    """Thread-safe: reads, cancels and transactions arrive from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Entry] = {}
        self._outcomes: dict[str, dict[tuple[str, str], _Outcome]] = {}
        self._kept: OrderedDict[tuple[str, str, str], _Outcome] = OrderedDict()

    def add(self, kind: str, value: object, owner: str | None = None) -> str:
        identifier = f"{kind}-{secrets.token_hex(16)}"
        with self._lock:
            self._entries[identifier] = Entry(kind, value, owner)
            self._outcomes[identifier] = {}
        return identifier

    def entry(self, kind: str, identifier: str) -> Entry:
        with self._lock:
            found = self._entries.get(identifier)
        if found is None or found.kind != kind:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"No open {kind} has this handle.",
                {"handle": kind},
            )
        return found

    def get(self, kind: str, identifier: str) -> object:
        return self.entry(kind, identifier).value

    def remove(self, identifier: str) -> Entry | None:
        """Let a handle go, with the outcomes of the calls made on it."""
        with self._lock:
            found = self._entries.pop(identifier, None)
            self._outcomes.pop(identifier, None)
        return found

    def ids(self, kind: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(i for i, e in self._entries.items() if e.kind == kind))

    def cancelled(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(i for i, e in self._entries.items() if e.cancelled))

    def once[T](self, scope: str, operation: str, key: str | None, work: Callable[[], T]) -> T:
        """`work`'s outcome for `key` on `scope`: run the first time, repeated after that."""
        if key is None:
            return work()
        name = (scope, operation, key)
        with self._lock:
            if scope == STORE_SCOPE:
                known = self._kept.get(name)
            else:
                known = self._outcomes.get(scope, {}).get((operation, key))
        if known is None:
            try:
                known = _Outcome(value=work())
            except StoreError as error:
                known = _Outcome(error=error)
            with self._lock:
                if scope == STORE_SCOPE:
                    known = self._kept.setdefault(name, known)
                    while len(self._kept) > KEPT:
                        self._kept.popitem(last=False)
                elif scope in self._outcomes:
                    known = self._outcomes[scope].setdefault((operation, key), known)
        if known.error is not None:
            raise known.error
        return known.value  # type: ignore[return-value]
