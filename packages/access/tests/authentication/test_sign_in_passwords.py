"""#438 AC1, R1, AC6: passwords are Argon2id hashes that carry their parameters, a weaker one is
replaced at sign-in, the rules refuse what they should, and an unknown user and a wrong password
are told apart neither by the answer nor by its time."""

from __future__ import annotations

import json
import statistics
import time

import pytest
from argon2 import PasswordHasher, Type
from orvex.access.authentication import credentials, passwords
from orvex.access.authentication.service import SignIn
from orvex.access.errors import LOGIN_FAILED, PASSWORD_REJECTED, AccessError

from tests.support.access_directory import World
from tests.support.sign_in import ADDRESS, PASSWORD, Accounts, Clock


def stored_hash(world: World, user: str) -> str:
    with world.store.transaction(read_only=True) as transaction:
        found = credentials.of(transaction, user)
    assert found is not None
    return found.hash


FULL_WIDTH = "".join(chr(ord(c) + 0xFEE0) if c != "-" else c for c in "Ana-perez-Lopez")


@pytest.mark.parametrize(
    ("password", "rule"),
    [
        ("eleven char", "length"),
        ("x" * 1025, "length"),
        ("ANA-PEREZ-LOPEZ", "user_name"),
        (FULL_WIDTH, "user_name"),
    ],
)
def test_the_rules_refuse_a_password(password: str, rule: str) -> None:
    with pytest.raises(AccessError) as caught:
        passwords.checked(password, "ana-perez-lopez")
    assert caught.value.code == PASSWORD_REJECTED
    assert caught.value.details == {"rule": rule}
    assert password not in str(caught.value)


def test_twelve_characters_with_no_composition_rules_are_enough() -> None:
    assert passwords.checked("aaaaaaaaaaaa", "ana") == "aaaaaaaaaaaa"


def test_a_hash_is_argon2id_and_carries_its_parameters(accounts: Accounts, world: World) -> None:
    ana = accounts.with_password("ana")
    stored = stored_hash(world, ana)
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert PASSWORD not in stored


def test_a_weaker_hash_is_replaced_at_the_next_sign_in(
    accounts: Accounts, world: World, sign_in: SignIn, clock: Clock
) -> None:
    ana = accounts.with_password("ana")
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    with world.store.transaction() as transaction:
        credentials.store(transaction, ana, weak.hash(PASSWORD), clock(), temporary=False)
    assert passwords.needs_rehash(stored_hash(world, ana))
    sign_in.login("ana", PASSWORD, ADDRESS)
    stronger = stored_hash(world, ana)
    assert not passwords.needs_rehash(stronger)
    assert passwords.verifies(stronger, PASSWORD)


def test_a_stored_value_that_is_not_argon2id_never_verifies(
    accounts: Accounts, world: World, sign_in: SignIn, clock: Clock
) -> None:
    ana = accounts.with_password("ana")
    other_kind = PasswordHasher(type=Type.I).hash(PASSWORD)
    with world.store.transaction() as transaction:
        transaction.run(
            "UPDATE credentials SET hash = :hash WHERE user_id = :user",
            hash=other_kind.replace("$argon2i$", "$argon2id$x"),
            user=ana,
        )
    with pytest.raises(AccessError) as caught:
        sign_in.login("ana", PASSWORD, ADDRESS)
    assert caught.value.code == LOGIN_FAILED
    assert not passwords.verifies(other_kind, PASSWORD)


def refusal(sign_in: SignIn, user_name: str, password: str) -> tuple[str, str, dict[str, str]]:
    with pytest.raises(AccessError) as caught:
        sign_in.login(user_name, password, ADDRESS)
    return caught.value.code, caught.value.message, dict(caught.value.details)


def test_an_unknown_user_and_a_wrong_password_get_the_same_answer(
    accounts: Accounts, sign_in: SignIn, clock: Clock, world: World
) -> None:
    accounts.with_password("ana")
    wrong = refusal(sign_in, "ana", "not the password")
    clock.move(days=2)
    unknown = refusal(sign_in, "nobody", "not the password")
    assert wrong == unknown == (LOGIN_FAILED, "The user name or the password is wrong.", {})
    reasons = [
        event["details"]["reason"]
        for event in world.events()
        if event["kind"] == "auth.login_failed"
    ]
    assert reasons[-2:] == ["WRONG_PASSWORD", "UNKNOWN_USER"]  # the log alone says which


def test_an_unknown_user_costs_what_a_wrong_password_costs(
    accounts: Accounts, sign_in: SignIn, clock: Clock
) -> None:
    """Medians of interleaved attempts, each past any delay; the bound is a fifth of the median
    or 10 ms, far below the cost of one Argon2id hash, which an unknown user would otherwise
    skip."""
    accounts.with_password("ana")
    timings: dict[str, list[float]] = {"ana": [], "nobody": []}
    for _ in range(25):
        for name, kept in timings.items():
            clock.move(days=2)
            started = time.perf_counter()
            with pytest.raises(AccessError):
                sign_in.login(name, "not the password", ADDRESS)
            kept.append(time.perf_counter() - started)
    known, unknown = (statistics.median(kept) for kept in timings.values())
    assert abs(known - unknown) < max(0.010, known / 5)
    started = time.perf_counter()
    passwords.verify_nobody("not the password")
    assert unknown > (time.perf_counter() - started) / 2  # the unknown user did hash


def test_no_password_reaches_the_audit_log(
    accounts: Accounts, sign_in: SignIn, world: World
) -> None:
    created = world.directory.create_user(world.admin, "ben", "Ben")
    accounts.with_password("ana")
    with pytest.raises(AccessError):
        sign_in.login("ana", "a wrong password", ADDRESS)
    logged = json.dumps(world.events())
    for secret in (PASSWORD, "a wrong password", created.temporary_password):
        assert secret not in logged


def test_a_value_that_is_no_hash_must_be_replaced() -> None:
    assert passwords.needs_rehash("not a hash")


def test_a_user_without_a_password_cannot_sign_in(
    accounts: Accounts, world: World, sign_in: SignIn
) -> None:
    ana = accounts.with_password("ana")
    with world.store.transaction() as transaction:
        transaction.run("DELETE FROM credentials WHERE user_id = :user", user=ana)
        assert credentials.of(transaction, ana) is None
    with pytest.raises(AccessError) as caught:
        sign_in.login("ana", PASSWORD, ADDRESS)
    assert caught.value.code == LOGIN_FAILED
    assert world.events()[-1]["details"]["reason"] == "NO_PASSWORD"
