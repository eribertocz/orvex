"""The growing delay after failed sign-ins (application, #438, #449 Q4): per account and per
address, never a permanent lock-out.

- **Keys.** One counter per account, keyed by the user name in the one form it is looked up by
  (`names.canonical_user_name`), whether or not that user exists, so a delay says nothing about
  existence. One per address: the IPv4 address, or the IPv6 /64. Only a SHA-256 of each is
  stored.
- **Delay.** After `n` failures, `2^(n-1)` seconds from the last admitted one, at most 15
  minutes.
- **Refused during a delay.** The attempt is counted on the counters that already exist, so the
  next delay is longer, but no delay is moved and no counter is created: the `Retry-After` it
  answers holds, and a delayed address cannot fill the store with new account counters.
- **Reserved first.** An admitted attempt is counted as a failure before the password is checked,
  in one transaction with the check of the delay, so concurrent attempts on one account cannot
  all pass while the first is still hashing. A correct password then takes the attempt back
  (`succeeded`): the account's counter is cleared, the address's lowered by one.
- **Bounded.** A counter is forgotten a day after its last failure. At most `capacity` counters
  are kept: the oldest are dropped first, in batches of a tenth of the capacity, but never an
  account's while its delay is active. When only active account delays are left, a new key is
  not admitted (fail closed).
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta

from orvex.access.directory.names import canonical_user_name
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Transaction

CAP = timedelta(minutes=15)
FORGET_AFTER = timedelta(days=1)
CAPACITY = 100_000
_UNKNOWN_ADDRESS = "unknown"


@dataclass(frozen=True, slots=True)
class Keys:
    account: str
    address: str


def _digest(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()


def address_prefix(address: str | None) -> str:
    """The address an IPv4 client is counted under, or its IPv6 /64; one shared bucket for an
    address that cannot be read."""
    try:
        parsed_address = ipaddress.ip_address(address or "")
    except ValueError:
        return _UNKNOWN_ADDRESS
    if isinstance(parsed_address, ipaddress.IPv6Address):
        mapped = parsed_address.ipv4_mapped
        if mapped is None:
            return str(ipaddress.ip_network(f"{parsed_address}/64", strict=False))
        parsed_address = mapped
    return str(parsed_address)


def keys(user_name: str, address: str | None) -> Keys:
    canonical = canonical_user_name(user_name)
    account = canonical if canonical is not None else "\0not a user name\0" + user_name
    return Keys(_digest("account", account), _digest("address", address_prefix(address)))


def delay_after(failures: int) -> timedelta:
    if failures >= 11:  # 2^10 s is already past the cap
        return CAP
    return min(timedelta(seconds=2 ** (failures - 1)), CAP)


def _rows(
    transaction: Transaction, found: Keys
) -> dict[str, tuple[int, datetime, datetime] | None]:
    """Both counters, read in one statement; None for a key with no counter."""
    rows: dict[str, tuple[int, datetime, datetime] | None] = {
        found.account: None,
        found.address: None,
    }
    for key, failures, failed, until in transaction.rows(
        "SELECT key, failures, failed_at, delayed_until FROM login_delays "
        "WHERE key = :account OR key = :address",
        account=found.account,
        address=found.address,
    ):
        rows[str(key)] = (int(str(failures)), parsed(str(failed)), parsed(str(until)))
    return rows


def _live(row: tuple[int, datetime, datetime] | None, now: datetime) -> bool:
    return row is not None and now < row[1] + FORGET_AFTER


def admit(transaction: Transaction, found: Keys, now: datetime, capacity: int = CAPACITY) -> int:
    """Count this attempt; return the seconds to wait, or 0 when it may go on (then reserved as a
    failure on both keys)."""
    rows = _rows(transaction, found)
    live = {key: row for key, row in rows.items() if row is not None and _live(row, now)}
    waits = [row[2] - now for row in live.values() if now < row[2]]
    if waits:
        for key in live:
            transaction.run(
                "UPDATE login_delays SET failures = failures + 1 WHERE key = :key", key=key
            )
        return _seconds(max(waits))
    new = [key for key in rows if key not in live]
    if new and not _make_room(transaction, len(new), now, capacity, set(rows)):
        return _seconds(_earliest_end(transaction, now) - now)
    for key in rows:
        failures = live[key][0] + 1 if key in live else 1
        transaction.run(
            "INSERT INTO login_delays (key, account, failures, failed_at, delayed_until) "
            "VALUES (:key, :account, :failures, :now, :until) "
            "ON CONFLICT (key) DO UPDATE SET failures = excluded.failures, "
            "failed_at = excluded.failed_at, delayed_until = excluded.delayed_until",
            key=key,
            account=key == found.account,
            failures=failures,
            now=instant(now),
            until=instant(now + delay_after(failures)),
        )
    return 0


def succeeded(transaction: Transaction, found: Keys, now: datetime) -> None:
    """Take back the failure `admit` reserved for an attempt whose password was right."""
    transaction.run("DELETE FROM login_delays WHERE key = :key", key=found.account)
    transaction.run(
        "DELETE FROM login_delays WHERE key = :key AND failures <= 1", key=found.address
    )
    transaction.run(
        "UPDATE login_delays SET failures = failures - 1, delayed_until = :now WHERE key = :key",
        key=found.address,
        now=instant(now),
    )


def _seconds(wait: timedelta) -> int:
    whole = int(wait.total_seconds())
    return max(1, whole + (1 if wait.total_seconds() > whole else 0))


def _make_room(
    transaction: Transaction, needed: int, now: datetime, capacity: int, keep: set[str]
) -> bool:
    """Make room for `needed` more counters: drop forgotten ones, then the oldest droppable ones,
    a tenth of the capacity beyond what is needed, so a flood evicts in batches."""
    ((count,),) = transaction.rows("SELECT count(*) FROM login_delays")
    if int(str(count)) + needed <= capacity:
        return True
    transaction.run(
        "DELETE FROM login_delays WHERE failed_at < :forgotten",
        forgotten=instant(now - FORGET_AFTER),
    )
    ((count,),) = transaction.rows("SELECT count(*) FROM login_delays")
    excess = int(str(count)) + needed - capacity
    if excess <= 0:
        return True
    droppable = transaction.rows(
        "SELECT key FROM login_delays WHERE (account = :no OR delayed_until <= :now) "
        "ORDER BY failed_at, key LIMIT :limit",
        no=False,
        now=instant(now),
        limit=excess + capacity // 10 + len(keep),
    )
    dropped = [str(key) for (key,) in droppable if str(key) not in keep][: excess + capacity // 10]
    for key in dropped:
        transaction.run("DELETE FROM login_delays WHERE key = :key", key=key)
    return len(dropped) >= excess


def _earliest_end(transaction: Transaction, now: datetime) -> datetime:
    ((until,),) = transaction.rows(
        "SELECT min(delayed_until) FROM login_delays WHERE delayed_until > :now",
        now=instant(now),
    )
    return now + timedelta(seconds=1) if until is None else parsed(str(until))
