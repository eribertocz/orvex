"""The result-cache interface contract (#486 G1, ADR-0012 as amended): what any implementation of
the semantic result cache must do, its value types and how its size is counted.

`semantic` computes the key (the execution identity with the permission fingerprint, ADR-0030
§6) before it calls the cache, so no security rule is in an implementation. An implementation
is built by its composer as `Implementation(max_bytes, clock)`: `max_bytes` is its bound
(at least one byte) and `clock` the monotonic seconds its entries' age is measured on. Then:

- a `Use` without a key is a bypass: `get` finds nothing and `put` keeps nothing;
- `get` hits only an entry put under an equal key, never a prefix or any other key;
- an entry whose age on the clock is at least the `ttl` of the `get` that finds it is dropped,
  and that `get` misses;
- `size_bytes` is the sum of `entry_bytes` of the entries kept, never above `max_bytes`; an
  entry over a sixteenth of the bound is never kept;
- when an entry does not fit, the least recently used ones (put or hit) leave first;
- `put` never raises on a full cache, and `get` and `put` are safe from many threads at once;
- a hit returns the stored table with every value, type, NULL, zone and metadata exact, its
  withheld policies and its estimate unchanged.

A `put` under a key already kept replaces that entry, so one idempotency key has one effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.version import Version

ENTRY_ALLOWANCE = 1024  # bytes counted for an entry besides its table
LARGEST_SHARE = 16  # an entry over `max_bytes // LARGEST_SHARE` is never kept


class ResultCacheError(Exception):
    """The contract's error type: a stable code, a message and details. No operation declares
    a code, so any error an implementation raises is refused as undeclared."""

    def __init__(self, code: str, message: str, details: Mapping[str, str]) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details)


@dataclass(frozen=True, slots=True)
class Withheld:
    """One policy that gives the caller no row: the notice's code, and the policy's dataset (as
    the model writes it) and name. Only the notice's own audience reaches a caller."""

    code: str
    dataset: str
    policy: str


@dataclass(frozen=True, slots=True)
class Use:
    """How one question uses the cache: its key (None: bypass), and its entries' lifetime in
    seconds (None: no `ttl`)."""

    key: str | None
    ttl: float | None


@dataclass(frozen=True, slots=True)
class Stored:
    """What one read returned, its checks applied, before any page or row was cut: the table,
    the policies that withheld every row and the read-cost estimate (None: none)."""

    table: pa.Table
    withheld: tuple[Withheld, ...]
    estimated: dict[str, int] | None


def entry_bytes(stored: Stored) -> int:
    """The bytes one entry counts against the bound."""
    return stored.table.nbytes + ENTRY_ALLOWANCE


class ResultCache(Protocol):
    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None: ...

    def put(self, envelope: CallEnvelope, use: Use, stored: Stored) -> None: ...

    def size_bytes(self, envelope: CallEnvelope) -> int: ...

    def entries(self, envelope: CallEnvelope) -> int: ...


RESULT_CACHE = InterfaceContract(
    name="result-cache",
    version=Version(1, 0),
    protocol=ResultCache,
    error_type=ResultCacheError,
    capabilities=("lookup", "store", "measure"),
    errors=(),
    operations=(
        Operation("get", "lookup"),
        Operation("put", "store", changes_state=True),
        Operation("size_bytes", "measure"),
        Operation("entries", "measure"),
    ),
)
