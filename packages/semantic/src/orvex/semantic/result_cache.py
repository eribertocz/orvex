"""The result cache (P3-10, #444; ADR-0012 as amended): the result-cache contract's
implementation (#486 G1) and the key of each question's use of it. What a question's one read
returned is kept in memory, keyed by the execution identity, so the same question with the same
identity is answered without reading again.

- **Where it sits.** Between planning and the read (`execution`): every question is bound,
  checked and planned with the caller's current context first, so every refusal and policy is
  decided anew. Only the read is skipped. Errors and refusals are never stored.
- **The key** is the execution identity (ADR-0030 §6) of this request: the compiled model, the
  query, the permission fingerprint built for it, every data version it reads (the snapshot its
  engine is pinned to, never a global latest one), the engine and adapter versions, and the
  bound statement with its parameters. Nothing in it comes from the request untrusted.
- **What is kept** is the read's table, the policies that withheld every row and the read-cost
  estimate. A hit rebuilds the page, its cursor (for this request's binding) and the receipt
  (`cache: hit`, sealed anew) exactly as a miss does: no token is ever handed out twice.
- **Off** for a question reading a dataset with `cache.enabled: false`, or any data without a
  retained version (a live engine): `bypass`. A dataset's `cache.ttl` expires an entry; a
  `ttl` in years or months has no fixed length, and one of zero keeps nothing, so their
  datasets are not cached. Resource settings (the memory limit, the timeout) are not in the
  key: they bound the process, not the answer.
- **Bounds.** One cache per process, least recently used first out, bounded in bytes; an
  entry over a sixteenth of the bound is not kept. Validity never depends on a message: an
  entry from an earlier snapshot is simply never matched again.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.semantic.interface import LARGEST_SHARE, Stored, Use, entry_bytes
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.ossie import sql
from orvex.semantic.receipt import DataRead

DEFAULT_BYTES = 256 * 2**20
DURATION = re.compile(
    r"^P(?:(?P<Y>[0-9]+)Y)?(?:(?P<M>[0-9]+)M)?(?:(?P<W>[0-9]+)W)?(?:(?P<D>[0-9]+)D)?"
    r"(?:T(?:(?P<h>[0-9]+)H)?(?:(?P<m>[0-9]+)M)?(?:(?P<s>[0-9]+)S)?)?$"
)
SECONDS = {"W": 604800, "D": 86400, "h": 3600, "m": 60, "s": 1}


class ResultCache:
    """One process's cache, the result-cache contract's implementation
    (`orvex.contracts.semantic.interface`). Thread-safe; it never raises on a full cache: it
    evicts."""

    def __init__(
        self, max_bytes: int = DEFAULT_BYTES, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("a result cache holds at least one byte")
        self.max_bytes = max_bytes
        self.clock = clock
        # Each entry: what was stored, when (on `clock`), and its bytes.
        self._entries: OrderedDict[str, tuple[Stored, float, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def size_bytes(self, envelope: CallEnvelope) -> int:
        with self._lock:
            return self._bytes

    def entries(self, envelope: CallEnvelope) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None:
        if use.key is None:
            return None
        with self._lock:
            found = self._entries.get(use.key)
            if found is None:
                return None
            if use.ttl is not None and self.clock() - found[1] >= use.ttl:
                self._drop(use.key)
                return None
            self._entries.move_to_end(use.key)
            return found[0]

    def put(self, envelope: CallEnvelope, use: Use, stored: Stored) -> None:
        if use.key is None:
            return
        size = entry_bytes(stored)
        if size > self.max_bytes // LARGEST_SHARE:
            return
        with self._lock:
            if use.key in self._entries:
                self._drop(use.key)
            self._entries[use.key] = (stored, self.clock(), size)
            self._bytes += size
            while self._bytes > self.max_bytes:
                self._drop(next(iter(self._entries)))

    def _drop(self, key: str) -> None:
        size = self._entries.pop(key)[2]
        self._bytes -= size


def use_of(
    compiled: CompiledModel,
    datasets: Iterable[str],
    execution: str,
    statement: str,
    parameters: Sequence[Mapping[str, str | None]],
    data: Sequence[DataRead],
) -> Use:
    """The cache use of one question: its key, from `execution` (the execution identity of
    this request) and the bound statement, or `bypass` when a dataset it reads turns the cache
    off or a read has no retained version."""
    if any(not read.retained or read.snapshot_id is None for read in data):
        return Use(None, None)
    settings = _settings(compiled)
    ttls: list[float] = []
    for dataset in datasets:
        found = settings.get(sql.normalize(dataset))
        if found is None:
            continue
        if found.get("enabled") is False:
            return Use(None, None)
        if "ttl" in found:
            seconds = _seconds(str(found["ttl"]))
            if not seconds:  # no fixed length, or zero: never kept
                return Use(None, None)
            ttls.append(seconds)
    key = fingerprint(
        "result-cache",
        1,
        {"execution": execution, "statement": statement, "parameters": list(map(dict, parameters))},
    )
    return Use(key, min(ttls) if ttls else None)


def _settings(compiled: CompiledModel) -> dict[str, Mapping[str, Any]]:
    """Each dataset's `cache` extension, by its normalized name."""
    found: dict[str, Mapping[str, Any]] = {}
    datasets = compiled.ossie.document.get("datasets", ())
    for extension in compiled.ossie.product_extensions:
        payload = extension.payload
        if payload.get("element") != "dataset" or "cache" not in payload:
            continue
        owner = extension.pointer.rsplit("/custom_extensions/", 1)[0]
        dataset = datasets[int(owner.rsplit("/", 1)[1])]  # type: ignore[index]
        settings = payload["cache"]
        if isinstance(settings, Mapping):
            found[sql.normalize(str(dataset["name"]))] = settings
    return found


def _seconds(duration: str) -> float | None:
    """An ISO 8601 duration in seconds; None for one in years or months (no fixed length)."""
    match = DURATION.fullmatch(duration)
    if match is None or match["Y"] or match["M"]:
        return None
    return float(sum(int(match[k] or 0) * n for k, n in SECONDS.items()))
