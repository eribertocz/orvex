"""#438 AC2, AC5, AC6, R2: each way a session ends, with a clock the test moves; ids from before a
sign-in or a rotation refused; temporary passwords; ids and passwords never in the audit log."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

import pytest
from orvex.access.authentication import sessions
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    LOGIN_FAILED,
    PASSWORD_REJECTED,
    SESSION_EXPIRED,
    AccessError,
)

from tests.support.access_directory import World
from tests.support.sign_in import ADDRESS, PASSWORD, Accounts, Clock


def ended(sign_in: SignIn, session_id: str) -> bool:
    try:
        sign_in.session(session_id)
    except AccessError as error:
        return error.code == SESSION_EXPIRED and not error.details  # never why
    return False


def last_ending(world: World) -> str:
    found = [e for e in world.events() if e["kind"] == "auth.session_expired"]
    return str(found[-1]["details"]["reason"])


def test_a_session_is_live_until_logout(accounts: Accounts, sign_in: SignIn, world: World) -> None:
    ana = accounts.with_password("ana")
    signed = sign_in.login("ana", PASSWORD, ADDRESS)
    assert sign_in.session(signed.session_id).user.id == ana
    assert not signed.password_change_only
    sign_in.logout(signed.session_id)
    assert ended(sign_in, signed.session_id)
    kinds = [e["kind"] for e in world.events()]
    assert kinds[-2:] == ["auth.login", "auth.logout"]
    sign_in.logout(signed.session_id)  # already gone: nothing to do


def test_twelve_hours_after_sign_in_a_session_ends_however_active(
    accounts: Accounts, sign_in: SignIn, clock: Clock, world: World
) -> None:
    accounts.with_password("ana")
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    for _ in range(14):
        clock.move(minutes=51)
        assert not ended(sign_in, session_id)  # 11 h 54 min, never idle
    clock.move(minutes=6)
    assert ended(sign_in, session_id)
    assert last_ending(world) == "absolute"


def test_an_hour_without_a_request_ends_a_session(
    accounts: Accounts, sign_in: SignIn, clock: Clock, world: World
) -> None:
    accounts.with_password("ana")
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    clock.move(minutes=59)
    assert not ended(sign_in, session_id)
    clock.move(minutes=59)
    assert not ended(sign_in, session_id)
    clock.move(minutes=60)
    assert ended(sign_in, session_id)
    assert last_ending(world) == "idle"


def test_the_administrators_lifetimes_apply(
    accounts: Accounts, sign_in: SignIn, settings: Settings, clock: Clock, world: World
) -> None:
    accounts.with_password("ana")
    settings.change(world.admin, absolute=timedelta(hours=1), idle=timedelta(minutes=10))
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    clock.move(minutes=10)
    assert ended(sign_in, session_id)


def deactivate(world: World, user: str) -> None:
    world.directory.deactivate_user(world.admin, user)


def make_administrator(world: World, user: str) -> None:
    world.directory.set_administrator(world.admin, user, True)


def add_to_a_project(world: World, user: str) -> None:
    builder = world.user("builder")
    world.project_with_builder("p1", builder)
    world.directory.add_membership(builder, "p1", user, "viewer")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (deactivate, "revoked"),
        (make_administrator, "permissions_changed"),
        (add_to_a_project, "permissions_changed"),
    ],
)
def test_a_change_to_the_user_ends_their_sessions_at_once(
    accounts: Accounts,
    sign_in: SignIn,
    world: World,
    change: Callable[[World, str], None],
    reason: str,
) -> None:
    ana = accounts.with_password("ana")
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    change(world, ana)
    assert ended(sign_in, session_id)
    assert last_ending(world) == reason


def test_a_session_presented_at_sign_in_is_not_accepted_after_it(
    accounts: Accounts, sign_in: SignIn
) -> None:
    accounts.with_password("ana")
    before = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    after = sign_in.login("ana", PASSWORD, ADDRESS, presented=before).session_id
    assert after != before
    assert ended(sign_in, before)
    assert not ended(sign_in, after)


def test_a_password_change_ends_every_other_session_and_renews_its_own(
    accounts: Accounts, sign_in: SignIn, world: World, clock: Clock
) -> None:
    accounts.with_password("ana")
    here = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    elsewhere = sign_in.login("ana", PASSWORD, "198.51.100.9").session_id
    renewed = sign_in.change_password(here, PASSWORD, "a brand new passphrase", ADDRESS)
    assert ended(sign_in, here)  # the old id after rotation
    assert ended(sign_in, elsewhere)
    assert not ended(sign_in, renewed)
    assert world.events()[-1]["kind"] == "user.password_changed"
    with pytest.raises(AccessError):
        sign_in.login("ana", PASSWORD, "192.0.2.1")
    clock.move(seconds=1)  # the account's delay after that failure
    sign_in.login("ana", "a brand new passphrase", "192.0.2.2")


def test_a_password_change_needs_the_current_password_and_the_rules(
    accounts: Accounts, sign_in: SignIn
) -> None:
    accounts.with_password("ana")
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    with pytest.raises(AccessError) as caught:
        sign_in.change_password(session_id, "not the password", "a brand new passphrase", ADDRESS)
    assert caught.value.code == LOGIN_FAILED
    with pytest.raises(AccessError) as caught:
        sign_in.change_password(session_id, PASSWORD, "too short", ADDRESS)
    assert caught.value.code == PASSWORD_REJECTED
    assert not ended(sign_in, session_id)


def test_a_temporary_password_opens_a_session_that_must_change_it(
    world: World, sign_in: SignIn
) -> None:
    created = world.directory.create_user(world.admin, "ana", "Ana")
    signed = sign_in.login("ana", created.temporary_password, ADDRESS)
    assert signed.password_change_only
    assert sign_in.session(signed.session_id).password_change_only
    renewed = sign_in.change_password(
        signed.session_id, created.temporary_password, PASSWORD, ADDRESS
    )
    assert not sign_in.session(renewed).password_change_only


def test_a_temporary_password_expires_after_24_hours(
    world: World, sign_in: SignIn, clock: Clock
) -> None:
    created = world.directory.create_user(world.admin, "ana", "Ana")  # on the real clock
    clock.move(hours=24, minutes=1)
    with pytest.raises(AccessError) as caught:
        sign_in.login("ana", created.temporary_password, ADDRESS)
    assert caught.value.code == LOGIN_FAILED
    assert world.events()[-1]["details"]["reason"] == "PASSWORD_EXPIRED"


def test_a_reset_ends_every_session_and_sets_a_temporary_password(
    accounts: Accounts, sign_in: SignIn, world: World, clock: Clock
) -> None:
    ana = accounts.with_password("ana")
    one = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    two = sign_in.login("ana", PASSWORD, "198.51.100.9").session_id
    temporary = sign_in.reset_password(world.admin, ana)
    assert ended(sign_in, one)
    assert ended(sign_in, two)
    assert world.events()[-1]["kind"] == "user.password_reset"
    clock.move(days=2)
    with pytest.raises(AccessError):
        sign_in.login("ana", PASSWORD, ADDRESS)  # the old password is gone
    clock.move(days=2)
    with pytest.raises(AccessError):
        sign_in.login("ana", temporary, ADDRESS)  # and the temporary one expired
    assert sign_in.reset_password(world.admin, ana) != temporary


def test_only_an_administrator_resets_someone_elses_password(
    accounts: Accounts, sign_in: SignIn, world: World
) -> None:
    ana = accounts.with_password("ana")
    ben = accounts.with_password("ben")
    for actor, target in ((ana, ben), (world.admin, world.admin)):
        with pytest.raises(AccessError) as caught:
            sign_in.reset_password(actor, target)
        assert caught.value.code == ACCESS_FORBIDDEN
    assert world.refusals()[-2:] == [ACCESS_FORBIDDEN, ACCESS_FORBIDDEN]


def test_an_unknown_or_overlong_session_id_is_expired(sign_in: SignIn) -> None:
    for session_id in ("", "x" * 43, "x" * 10_000):
        assert ended(sign_in, session_id)


def test_only_the_hash_of_a_session_id_is_stored(
    accounts: Accounts, sign_in: SignIn, world: World
) -> None:
    accounts.with_password("ana")
    session_id = sign_in.login("ana", PASSWORD, ADDRESS).session_id
    assert len(session_id) == 43  # 256 random bits
    with world.store.transaction(read_only=True) as transaction:
        stored = [str(row[0]) for row in transaction.rows("SELECT id_hash FROM sessions")]
    assert session_id not in stored
    assert sessions._hash(session_id) in stored
    assert session_id not in json.dumps(world.events())
