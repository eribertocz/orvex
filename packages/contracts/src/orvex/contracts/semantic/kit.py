"""The conformance kit of the result-cache contract (#486 G1): every guarantee of
`orvex.contracts.semantic.interface`, which any implementation must pass in full.

Run it with `verify_kit(RESULT_CACHE_KIT, maker(Implementation))`: each case gets a fresh
implementation built as `Implementation(max_bytes=BOUND, clock=CLOCK)`, with the kit's own
clock set back to zero, so a case moves time only by `CLOCK.advance`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.semantic.interface import (
    LARGEST_SHARE,
    RESULT_CACHE,
    Stored,
    Use,
    Withheld,
    entry_bytes,
)

BOUND = 64 * 1024  # the bound every case's implementation is built with
ZONE = "America/La_Paz"


class KitClock:
    """The kit's clock: monotonic seconds that only a case moves."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def reset(self) -> None:
        with self._lock:
            self._now = 0.0

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


CLOCK = KitClock()


def maker(implementation: Callable[..., object]) -> Callable[[], object]:
    """What the kit runs each case on: a fresh implementation, on the kit's clock at zero."""

    def make() -> object:
        CLOCK.reset()
        return implementation(max_bytes=BOUND, clock=CLOCK)

    return make


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def numbers(rows: int, first: int = 0) -> Stored:
    """A stored answer of `rows` 64-bit integers, no NULL: `8 * rows` bytes of table."""
    table = pa.table({"n": pa.array(range(first, first + rows), pa.int64())})
    return Stored(table, (), None)


def exact() -> Stored:
    """A stored answer with every exactness the contract keeps: DECIMAL scale, HUGEINT width,
    a zoned timestamp, 64-bit integers past a float's precision, NULLs, schema metadata, the
    withheld policies and the estimate."""
    schema = pa.schema(
        [
            pa.field("amount", pa.decimal128(18, 2)),
            pa.field("huge", pa.decimal128(38, 0)),
            pa.field("at", pa.timestamp("us", tz=ZONE)),
            pa.field("count", pa.int64()),
            pa.field("name", pa.string()),
        ],
        metadata={b"kit": b"exact"},
    )
    table = pa.table(
        {
            "amount": [Decimal("12.50"), None, Decimal("-0.01")],
            "huge": [Decimal(2**120), Decimal(-(2**100)), None],
            "at": [datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo(ZONE)), None, None],
            "count": [9_007_199_254_740_993, None, 0],
            "name": ["a", None, ""],
        },
        schema=schema,
    )
    withheld = (
        Withheld("POLICY_ATTRIBUTE_MISSING", "sales", "by_region"),
        Withheld("POLICY_MAPPING_MISSING", "sales", "by_vendor"),
    )
    return Stored(table, withheld, {"rows": 2**62 + 1, "files": 3})


def _same(found: Stored | None, stored: Stored) -> None:
    _expect(isinstance(found, Stored), f"expected a hit, found {found!r}")
    assert isinstance(found, Stored)
    _expect(isinstance(found.table, pa.Table), "the hit holds no table")
    _expect(
        found.table.schema.equals(stored.table.schema, check_metadata=True),
        f"the schema changed: {found.table.schema} for {stored.table.schema}",
    )
    # Arrow equality under an equal schema: every value bit for bit at its exact type (a
    # decimal's precision and scale, a timestamp's unit and zone) and every NULL where it was.
    _expect(found.table.equals(stored.table, check_metadata=True), "a value or a NULL changed")
    _expect(found.withheld == stored.withheld, f"the withheld policies changed: {found.withheld}")
    _expect(type(found.withheld) is tuple, "the withheld policies are not a tuple")
    _expect(found.estimated == stored.estimated, f"the estimate changed: {found.estimated}")
    _expect(
        found.estimated is None or all(type(v) is int for v in found.estimated.values()),
        "the estimate's numbers are not integers",
    )


def bypass_without_key(cache: KitClient) -> None:
    cache.put(Use(None, None), numbers(4))
    _expect(cache.get(Use(None, None)) is None, "a use without a key found an entry")
    _expect((cache.entries(), cache.size_bytes()) == (0, 0), "a use without a key kept an entry")


def hit_only_equal_key(cache: KitClient) -> None:
    stored = numbers(4)
    cache.put(Use("key-abc", None), stored)
    for other in ("key-ab", "key-abcd", "key-abd", "KEY-ABC", "key-abc "):
        _expect(cache.get(Use(other, None)) is None, f"{other!r} found the entry of 'key-abc'")
    _same(cache.get(Use("key-abc", None)), stored)
    _same(cache.get(Use("key-abc", 60.0)), stored)


def exact_hit(cache: KitClient) -> None:
    stored = exact()
    cache.put(Use("exact", None), stored)
    _same(cache.get(Use("exact", None)), stored)
    _same(cache.get(Use("exact", None)), stored)


def expires_by_ttl(cache: KitClient) -> None:
    stored = numbers(4)
    cache.put(Use("short", 10.0), stored)
    CLOCK.advance(9.999)
    _same(cache.get(Use("short", 10.0)), stored)
    CLOCK.advance(0.001)
    _expect(cache.get(Use("short", 10.0)) is None, "an entry was found at its ttl")
    _expect((cache.entries(), cache.size_bytes()) == (0, 0), "an expired entry is still counted")
    cache.put(Use("long", None), stored)
    CLOCK.advance(10**9)
    _same(cache.get(Use("long", None)), stored)


def bounded_never_raises(cache: KitClient) -> None:
    for index in range(100):
        cache.put(Use(f"entry-{index}", None), numbers(256, index))
        size = cache.size_bytes()
        _expect(size <= BOUND, f"{size} bytes kept for a bound of {BOUND}")
    each = entry_bytes(numbers(256))
    room = BOUND // each
    _expect(cache.entries() == room, f"{cache.entries()} entries kept, {room} fit")
    _expect(cache.size_bytes() == cache.entries() * each, "the bytes kept are not the entries'")


def largest_share(cache: KitClient) -> None:
    largest = BOUND // LARGEST_SHARE
    fits = numbers((largest - entry_bytes(numbers(0))) // 8)
    over = numbers(len(fits.table) + 1)
    _expect(entry_bytes(fits) == largest, "the kit's entry is not exactly the largest share")
    cache.put(Use("over", None), over)
    _expect(cache.get(Use("over", None)) is None, "an entry over a sixteenth was kept")
    _expect((cache.entries(), cache.size_bytes()) == (0, 0), "an entry over a sixteenth counted")
    cache.put(Use("fits", None), fits)
    _same(cache.get(Use("fits", None)), fits)


def least_recently_used_first(cache: KitClient) -> None:
    each = entry_bytes(numbers(256))
    room = BOUND // each
    for index in range(room):
        cache.put(Use(f"entry-{index}", None), numbers(256, index))
    _expect(cache.entries() == room, f"{cache.entries()} entries kept, {room} fit")
    _same(cache.get(Use("entry-0", None)), numbers(256, 0))
    cache.put(Use("entry-new", None), numbers(256, room))
    _expect(cache.get(Use("entry-1", None)) is None, "the least recently used entry stayed")
    _same(cache.get(Use("entry-0", None)), numbers(256, 0))
    _same(cache.get(Use("entry-2", None)), numbers(256, 2))
    cache.put(Use("entry-newer", None), numbers(256, room + 1))
    _expect(cache.get(Use("entry-3", None)) is None, "a recently read entry left before it")


def replaced_by_put(cache: KitClient) -> None:
    cache.put(Use("same", None), numbers(256))
    cache.put(Use("same", None), numbers(4, 7))
    _same(cache.get(Use("same", None)), numbers(4, 7))
    _expect(cache.entries() == 1, "a key put twice holds two entries")
    _expect(cache.size_bytes() == entry_bytes(numbers(4)), "a replaced entry is still counted")


def size_counts_entries(cache: KitClient) -> None:
    _expect(cache.size_bytes() == 0, "an empty cache counts bytes")
    cache.put(Use("a", None), numbers(4))
    cache.put(Use("b", None), exact())
    expected = entry_bytes(numbers(4)) + entry_bytes(exact())
    _expect(cache.size_bytes() == expected, f"{cache.size_bytes()} bytes, expected {expected}")


def entries_count_keys(cache: KitClient) -> None:
    _expect(cache.entries() == 0, "an empty cache has entries")
    for key in ("a", "b", "a"):
        cache.put(Use(key, None), numbers(4))
    _expect(cache.entries() == 2, f"{cache.entries()} entries for two keys")


def thread_safe(cache: KitClient) -> None:
    failures: list[str] = []
    each = entry_bytes(numbers(64))

    def work(worker: int) -> None:
        try:
            for index in range(40):
                key = f"w{worker}-{index % 13}"
                stored = numbers(64, worker * 1000 + index % 13)
                cache.put(Use(key, None), stored)
                found = cache.get(Use(key, None))
                if found is not None and not found.table.equals(stored.table):
                    failures.append(f"{key} answered another entry's table")
                if cache.size_bytes() > BOUND:
                    failures.append("the bound was exceeded")
        except Exception as error:  # noqa: BLE001 - any error in a thread fails the case
            failures.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=work, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _expect(not failures, "; ".join(sorted(set(failures))))
    _expect(cache.size_bytes() == cache.entries() * each, "concurrent calls broke the byte count")
    kept = cache.entries()
    _expect(0 < kept <= BOUND // each, f"{kept} entries after concurrent calls")


STORED = numbers(4)


def put_once(cache: KitClient, envelope: CallEnvelope) -> object:
    return cache.send("put", envelope, Use("idempotent", None), STORED)


def observe(cache: KitClient) -> object:
    return (cache.entries(), cache.size_bytes())


RESULT_CACHE_KIT = Kit(
    contract=RESULT_CACHE,
    recorded=12,
    cases=(
        KitCase(
            "get.bypass-without-key",
            "get",
            "a use without a key finds and keeps nothing",
            bypass_without_key,
        ),
        KitCase(
            "get.equal-key-only",
            "get",
            "a hit needs an equal key: no prefix, extension, case or space variant",
            hit_only_equal_key,
        ),
        KitCase(
            "get.exact-hit",
            "get",
            "a hit returns the table with every value, type, NULL, zone, scale and metadata "
            "exact, its withheld policies and its estimate unchanged",
            exact_hit,
        ),
        KitCase(
            "get.expires-by-ttl",
            "get",
            "an entry is found before its ttl on the injected clock, never at or after it, "
            "and an entry without a ttl never expires",
            expires_by_ttl,
        ),
        KitCase(
            "get.least-recently-used-first",
            "get",
            "when an entry does not fit, the least recently put or hit leaves first",
            least_recently_used_first,
        ),
        KitCase(
            "get.thread-safe",
            "get",
            "concurrent get and put never raise, mix entries or break the byte count",
            thread_safe,
        ),
        KitCase(
            "put.bounded-never-raises",
            "put",
            "a full cache evicts and never raises, and never holds more than its bound",
            bounded_never_raises,
        ),
        KitCase(
            "put.largest-share",
            "put",
            "an entry over a sixteenth of the bound is never kept; one of exactly a sixteenth is",
            largest_share,
        ),
        KitCase(
            "put.replaces-equal-key",
            "put",
            "a put under a kept key replaces its entry and its bytes",
            replaced_by_put,
        ),
        KitCase(
            "put.idempotent",
            "put",
            "a put sent twice with one idempotency key has one effect",
            Idempotent(put_once, observe),
        ),
        KitCase(
            "size-bytes.counts-entries",
            "size_bytes",
            "the bytes are the sum of entry_bytes of the entries kept",
            size_counts_entries,
        ),
        KitCase(
            "entries.counts-keys",
            "entries",
            "the number of entries is the number of keys kept",
            entries_count_keys,
        ),
    ),
)
