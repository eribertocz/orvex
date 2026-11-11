"""#438 AC4, R3: the delay after failed sign-ins grows per account and per address up to its cap,
never locks an account for good, stays within its bounded store under a flood, and counts
concurrent failures exactly."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from orvex.access.authentication import delays
from orvex.access.authentication.service import SignIn
from orvex.access.errors import LOGIN_DELAYED, LOGIN_FAILED, AccessError

from tests.support.access_directory import World
from tests.support.sign_in import ADDRESS, PASSWORD, Accounts, Clock


def attempt(sign_in: SignIn, user_name: str, password: str, address: str = ADDRESS) -> str:
    """The outcome: `ok`, `LOGIN_FAILED`, or `LOGIN_DELAYED <seconds>`."""
    try:
        sign_in.login(user_name, password, address)
    except AccessError as error:
        if error.code == LOGIN_DELAYED:
            return f"{error.code} {error.details['retry_after']}"
        return error.code
    return "ok"


def test_the_delay_grows_per_account_up_to_fifteen_minutes(
    accounts: Accounts, sign_in: SignIn, clock: Clock
) -> None:
    """Each round: a wrong password from a fresh address, then the right one, refused during
    the delay and counted too; then a wait past the delay. Two failures a round."""
    accounts.with_password("ana")
    waits = []
    for number in range(1, 8):
        assert attempt(sign_in, "ana", "wrong", f"192.0.2.{number}") == LOGIN_FAILED
        waits.append(attempt(sign_in, "ana", PASSWORD, f"198.51.100.{number}"))
        clock.move(minutes=15)
    assert waits == [f"{LOGIN_DELAYED} {s}" for s in (1, 4, 16, 64, 256, 900, 900)]
    assert attempt(sign_in, "ana", PASSWORD, "198.51.100.99") == "ok"


def test_a_delay_doubles_with_each_failure_and_stops_at_its_cap() -> None:
    assert [delays.delay_after(n).total_seconds() for n in range(1, 13)] == [
        1,
        2,
        4,
        8,
        16,
        32,
        64,
        128,
        256,
        512,
        900,
        900,
    ]
    assert delays.delay_after(10**9) == timedelta(minutes=15)


def test_a_delayed_account_answers_the_same_for_a_known_and_an_unknown_user(
    accounts: Accounts, sign_in: SignIn
) -> None:
    accounts.with_password("ana")
    for name in ("ana", "nobody"):
        assert attempt(sign_in, name, "wrong", "192.0.2.1" if name == "ana" else "192.0.2.2") == (
            LOGIN_FAILED
        )
    known = attempt(sign_in, "ana", PASSWORD, "198.51.100.1")
    unknown = attempt(sign_in, "nobody", PASSWORD, "198.51.100.2")
    assert known == unknown == f"{LOGIN_DELAYED} 1"


def test_the_delay_grows_per_address_across_accounts(
    accounts: Accounts, sign_in: SignIn, clock: Clock
) -> None:
    accounts.with_password("ana")
    outcomes = [attempt(sign_in, name, "wrong", "2001:db8::1") for name in ("u1", "u2", "u3")]
    assert outcomes == [LOGIN_FAILED, f"{LOGIN_DELAYED} 1", f"{LOGIN_DELAYED} 2"]
    # Another address in the same /64 is the same address, though the account is fresh.
    assert attempt(sign_in, "ana", PASSWORD, "2001:db8::ffff") == f"{LOGIN_DELAYED} 4"
    clock.move(seconds=1)  # the account's own delay from that refusal
    assert attempt(sign_in, "ana", PASSWORD, "2001:db8:0:1::1") == "ok"


def test_no_account_is_locked_for_good(accounts: Accounts, sign_in: SignIn, clock: Clock) -> None:
    accounts.with_password("ana")
    for _ in range(30):
        attempt(sign_in, "ana", "wrong", "192.0.2.1")
    assert attempt(sign_in, "ana", PASSWORD, "192.0.2.9").startswith(LOGIN_DELAYED)
    clock.move(minutes=15)
    assert attempt(sign_in, "ana", PASSWORD, "192.0.2.9") == "ok"


def test_a_right_password_takes_its_reservation_back(
    accounts: Accounts, sign_in: SignIn, world: World
) -> None:
    accounts.with_password("ana")
    for _ in range(3):
        assert attempt(sign_in, "ana", PASSWORD) == "ok"
    with world.store.transaction(read_only=True) as transaction:
        assert transaction.rows("SELECT key FROM login_delays") == []


def test_a_flood_of_addresses_stays_within_the_cap_and_keeps_an_active_account_delay(
    world: World, clock: Clock
) -> None:
    capacity = 200
    now = clock()
    ana = delays.keys("ana", "192.0.2.1")
    with world.store.transaction() as transaction:
        for _ in range(10):
            delays.admit(transaction, ana, now, capacity)
    flood = [
        delays.keys("nobody", f"10.{n // 65536}.{n // 256 % 256}.{n % 256}") for n in range(10_000)
    ]
    for start in range(0, len(flood), 1000):
        with world.store.transaction() as transaction:
            for keys in flood[start : start + 1000]:
                delays.admit(transaction, keys, now, capacity)  # ana's delay stays active
    with world.store.transaction(read_only=True) as transaction:
        ((count,),) = transaction.rows("SELECT count(*) FROM login_delays")
        kept = transaction.rows(
            "SELECT failures FROM login_delays WHERE key = :key", key=ana.account
        )
    assert int(str(count)) <= capacity
    assert [int(str(row[0])) for row in kept] == [10]


def test_a_full_store_of_active_account_delays_admits_no_new_key(
    world: World, clock: Clock
) -> None:
    now = clock()
    with world.store.transaction() as transaction:
        for number in range(2):
            keys = delays.keys(f"user{number}", "192.0.2.1")
            delays.admit(transaction, keys, now, capacity=10)
            delays.admit(transaction, keys, now, capacity=10)
        wait = delays.admit(transaction, delays.keys("new", "192.0.2.2"), now, capacity=3)
    assert wait >= 1  # fail closed: refused rather than counted nowhere


def test_fifty_concurrent_wrong_passwords_count_fifty_failures(
    accounts: Accounts, sign_in: SignIn, world: World
) -> None:
    ana = accounts.with_password("ana")
    start = threading.Barrier(50)

    def wrong(number: int) -> str:
        start.wait(timeout=60)
        return attempt(sign_in, "ana", "wrong", f"192.0.2.{number}")

    with ThreadPoolExecutor(max_workers=50) as pool:
        outcomes = list(pool.map(wrong, range(50)))
    assert all(outcome == LOGIN_FAILED or outcome.startswith(LOGIN_DELAYED) for outcome in outcomes)
    with world.store.transaction(read_only=True) as transaction:
        ((failures,),) = transaction.rows(
            "SELECT failures FROM login_delays WHERE key = :key",
            key=delays.keys("ana", None).account,
        )
    assert int(str(failures)) == 50
    failed = [
        event
        for event in world.events()
        if event["kind"] == "auth.login_failed" and event["actor"] == ana
    ]
    assert len(failed) == 50


@pytest.mark.parametrize(
    ("address", "counted_as"),
    [
        ("192.0.2.7", "192.0.2.7"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("not an address", "unknown"),
        (None, "unknown"),
    ],
)
def test_an_address_is_counted_by_its_prefix(address: str | None, counted_as: str) -> None:
    assert delays.address_prefix(address) == counted_as
