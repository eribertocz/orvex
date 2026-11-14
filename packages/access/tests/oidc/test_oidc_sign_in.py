"""#439 AC2-AC6, AC8-AC10, R1, R3, R4: the OIDC flow against an in-process provider. Each check
of the callback has its own planted failure and audit reason; identity is (issuer, subject);
nothing links itself; the mapping sets only the groups it manages; keys are read at most once a
minute; secrets reach no record; provider groups are re-read within `group_refresh`."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from orvex.access.errors import OIDC_FAILED, AccessError
from orvex.access.oidc.configuration import parsed
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn

from tests.support.access_directory import World
from tests.support.oidc_provider import (
    FakeFetcher,
    FakeProvider,
    Signer,
    hmac_with_secret,
    signed_by_stranger,
    unsigned,
)
from tests.support.oidc_world import ENVIRONMENT, Oidc, document


def linked(oidc: Oidc, name: str = "ana", subject: str = "sub-ana") -> str:
    user = oidc.world.user(name)
    oidc.identities.link(oidc.world.admin, user, "corp", subject)
    return user


def link_of(oidc: Oidc, user: str) -> str:
    (found,) = oidc.identities.links(oidc.world.admin, user)
    return found.id


def reason_of(oidc: Oidc) -> str:
    failed = [e for e in oidc.world.events() if e["kind"] == "auth.login_failed"]
    return str(failed[-1]["details"]["reason"])


def refused(oidc: Oidc, action: Callable[[], object]) -> str:
    with pytest.raises(AccessError) as caught:
        action()
    assert caught.value.code == OIDC_FAILED
    assert caught.value.message == "Sign-in failed."
    return reason_of(oidc)


def test_a_linked_user_signs_in_and_gets_a_session(oidc: Oidc) -> None:
    ana = linked(oidc)
    signed = oidc.sign_in_as("sub-ana")
    assert signed.user.id == ana
    assert oidc.sign_in.session(signed.session_id).user.id == ana
    login = oidc.world.events()[-1]
    assert (login["kind"], login["details"]) == ("auth.login", {"method": "oidc"})
    (_, form, basic) = oidc.provider.posts[-1]
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"]
    assert basic[0] == "bi-client"


def test_the_start_asks_for_code_pkce_s256_and_query_mode(oidc: Oidc) -> None:
    started = oidc.service.start("corp")
    location = started.location
    assert location.startswith("https://idp.example.com/auth?")
    for part in (
        "response_type=code",
        "response_mode=query",
        "code_challenge_method=S256",
        "scope=openid",
        "redirect_uri=https%3A%2F%2Fbi.example.com%2Fapi%2Fv1%2Fauth%2Foidc%2Fcorp%2Fcallback",
    ):
        assert part in location
    assert "prompt=" not in location


def flow(oidc: Oidc, **change: str) -> Callable[[], object]:
    """A callback whose answer is altered: `drop` removes a parameter, others replace it."""
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    drop = change.pop("drop", None)
    if drop:
        answer.pop(drop)
    answer.update(change)
    return lambda: oidc.service.callback("corp", answer, started.reference)


def test_a_wrong_state_is_refused(oidc: Oidc) -> None:
    linked(oidc)
    assert refused(oidc, flow(oidc, state="x" * 43)) == "STATE"


def test_a_replayed_binding_is_refused(oidc: Oidc) -> None:
    linked(oidc)
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    oidc.service.callback("corp", answer, started.reference)
    again = oidc.provider.authorize(started.location, "sub-ana")
    assert refused(oidc, lambda: oidc.service.callback("corp", again, started.reference)) == "STATE"


def test_a_failed_callback_still_uses_its_binding_up(oidc: Oidc) -> None:
    linked(oidc)
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    refused(
        oidc,
        lambda: oidc.service.callback("corp", {**answer, "state": "y" * 43}, started.reference),
    )
    assert (
        refused(oidc, lambda: oidc.service.callback("corp", answer, started.reference)) == "STATE"
    )


def test_a_missing_or_old_binding_is_refused(oidc: Oidc) -> None:
    linked(oidc)
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    assert refused(oidc, lambda: oidc.service.callback("corp", answer, None)) == "STATE"
    late = oidc.service.start("corp")
    late_answer = oidc.provider.authorize(late.location, "sub-ana")
    oidc.clock.move(minutes=10)
    assert (
        refused(oidc, lambda: oidc.service.callback("corp", late_answer, late.reference)) == "STATE"
    )


def test_an_error_response_ends_the_flow(oidc: Oidc) -> None:
    linked(oidc)
    assert refused(oidc, flow(oidc, error="access_denied")) == "ERROR_RESPONSE"


def test_a_wrong_or_missing_iss_parameter_is_refused(oidc: Oidc) -> None:
    linked(oidc)
    assert refused(oidc, flow(oidc, iss="https://evil.example/realms/corp")) == "ISSUER"
    assert refused(oidc, flow(oidc, drop="iss")) == "ISSUER"  # advertised, so required


@pytest.mark.parametrize(
    ("forge", "reason"),
    [(unsigned, "SIGNATURE"), (hmac_with_secret, "SIGNATURE"), (signed_by_stranger, "SIGNATURE")],
    ids=["none", "HS256 with the client secret", "an unknown key"],
)
def test_a_forged_token_is_refused(oidc: Oidc, forge: Signer, reason: str) -> None:
    linked(oidc)
    oidc.provider.sign = forge
    assert refused(oidc, flow(oidc)) == reason


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://evil.example/realms/corp"}, "ISSUER"),
        ({"aud": "someone-else"}, "CLAIMS"),
        ({"aud": ["bi-client", "other"]}, "CLAIMS"),  # several audiences and no azp
        ({"aud": ["bi-client", "other"], "azp": "other"}, "CLAIMS"),
        ({"azp": "other"}, "CLAIMS"),
        ({"exp": int(time.time()) - 61}, "CLAIMS"),
        ({"nbf": int(time.time()) + 3600}, "CLAIMS"),
        ({"iat": int(time.time()) + 3600}, "CLAIMS"),
        ({"nonce": "n" * 43}, "NONCE"),
        ({"sub": ""}, "CLAIMS"),
    ],
    ids=[
        "iss",
        "aud",
        "audiences without azp",
        "azp among audiences",
        "azp",
        "exp",
        "nbf",
        "iat",
        "nonce",
        "sub",
    ],
)
def test_each_claim_check_refuses_its_planted_failure(
    oidc: Oidc, claims: dict[str, Any], reason: str
) -> None:
    linked(oidc)
    oidc.provider.claims_extra = claims
    assert refused(oidc, flow(oidc)) == reason


def test_sixty_seconds_of_clock_skew_are_tolerated(oidc: Oidc) -> None:
    linked(oidc)
    oidc.provider.claims_extra = {"exp": int(time.time()) - 30, "iat": int(time.time()) + 30}
    flow(oidc)()


def test_a_failed_token_exchange_is_refused(oidc: Oidc) -> None:
    linked(oidc)
    oidc.provider.token_answer = lambda code: b'{"access_token": "only"}'
    assert refused(oidc, flow(oidc)) == "TOKEN_EXCHANGE"
    oidc.provider.token_answer = None
    assert refused(oidc, flow(oidc, code="unknown")) == "TOKEN_EXCHANGE"


def test_the_identity_is_issuer_and_subject_never_email(world: World, oidc: Oidc) -> None:
    ana = linked(oidc, "ana", "shared-sub")
    other = FakeProvider(issuer="https://other.example.com/realms/x")
    config = parsed(
        {
            "providers": {
                **document()["providers"],  # type: ignore[dict-item]
                "other": {
                    "issuer": other.issuer,
                    "client_id": "bi-client",
                    "client_secret_variable": "CORP_SECRET",
                    "algorithms": ["RS256"],
                },
            }
        },
        ENVIRONMENT,
    )
    service = OidcSignIn(
        world.store, world.audit, config, FakeFetcher(oidc.provider, other), oidc.clock
    )
    started = service.start("other")
    answer = other.authorize(started.location, "shared-sub", email="ana@example.com")
    with pytest.raises(AccessError):
        service.callback("other", answer, started.reference)  # same sub, same email, other issuer
    assert reason_of(oidc) == "UNLINKED"
    assert oidc.sign_in_as("shared-sub").user.id == ana


def test_an_unlinked_identity_never_takes_over_a_local_user(oidc: Oidc) -> None:
    oidc.world.user("ana")
    action = lambda: oidc.sign_in_as("ana", email="ana@example.com", preferred_username="ana")  # noqa: E731
    assert refused(oidc, action) == "UNLINKED"


def test_a_deactivated_linked_user_is_refused(oidc: Oidc) -> None:
    ana = linked(oidc)
    oidc.world.directory.deactivate_user(oidc.world.admin, ana)
    assert refused(oidc, lambda: oidc.sign_in_as("sub-ana")) == "DEACTIVATED"
    failed = [e for e in oidc.world.events() if e["kind"] == "auth.login_failed"][-1]
    assert failed["actor"] == ana


def test_a_link_or_unlink_ends_the_users_sessions(oidc: Oidc) -> None:
    ana = linked(oidc)
    session_id = oidc.sign_in_as("sub-ana").session_id
    oidc.identities.unlink(oidc.world.admin, ana, link_of(oidc, ana))
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)
    assert refused(oidc, lambda: oidc.sign_in_as("sub-ana")) == "UNLINKED"
    kinds = [e["kind"] for e in oidc.world.events()]
    assert "user.oidc_linked" in kinds
    assert "user.oidc_unlinked" in kinds


def test_no_administrator_links_their_own_user(oidc: Oidc) -> None:
    with pytest.raises(AccessError):
        oidc.identities.link(oidc.world.admin, oidc.world.admin, "corp", "sub-root")


def groups(oidc: Oidc, user: str) -> set[str]:
    from orvex.access.directory import statements

    with oidc.world.store.transaction(read_only=True) as transaction:
        return set(statements.groups_of(transaction, "p1", user))


def with_project(oidc: Oidc, user: str) -> None:
    builder = oidc.world.user("builder")
    oidc.world.project_with_builder("p1", builder)
    for name in ("finance", "ops", "manual"):
        oidc.world.directory.create_group(builder, "p1", name)
    oidc.world.directory.add_membership(builder, "p1", user, "viewer")
    oidc.world.directory.add_group_member(builder, "p1", "manual", user)


def test_the_mapping_sets_only_the_groups_it_manages(oidc: Oidc) -> None:
    ana = linked(oidc)
    with_project(oidc, ana)
    oidc.sign_in_as("sub-ana", groups=["finance", "unmapped"])
    assert groups(oidc, ana) == {"finance", "manual"}
    oidc.sign_in_as("sub-ana", groups=["ops"])
    assert groups(oidc, ana) == {"ops", "manual"}  # finance removed, manual untouched
    oidc.sign_in_as("sub-ana")
    assert groups(oidc, ana) == {"manual"}


def test_no_group_is_granted_without_a_membership(oidc: Oidc) -> None:
    ana = linked(oidc)
    builder = oidc.world.user("builder")
    oidc.world.project_with_builder("p1", builder)
    oidc.world.directory.create_group(builder, "p1", "finance")
    oidc.sign_in_as("sub-ana", groups=["finance"])
    assert groups(oidc, ana) == set()


def test_unknown_keys_are_read_at_most_once_a_minute(oidc: Oidc) -> None:
    linked(oidc)
    keys = oidc.service._provider("corp")[1].keys
    before = keys.reads
    stranger = RSAKey.generate_key(2048, parameters={"kid": "unknown"})
    oidc.provider.sign = lambda header, body: jwt.encode(
        {**header, "kid": "unknown"}, body, stranger
    )
    for _ in range(1000):
        started = oidc.service.start("corp")
        answer = oidc.provider.authorize(started.location, "sub-ana")
        with pytest.raises(AccessError):
            oidc.service.callback("corp", answer, started.reference)
    assert keys.reads - before <= 1
    oidc.clock.move(minutes=1)
    with pytest.raises(AccessError):
        flow(oidc)()
    assert keys.reads - before <= 2


def test_no_token_code_verifier_or_secret_is_recorded(
    oidc: Oidc, caplog: pytest.LogCaptureFixture
) -> None:
    linked(oidc)
    with caplog.at_level("DEBUG"):
        oidc.sign_in_as("sub-ana")
        refused(oidc, flow(oidc, state="z" * 43))
    secrets = {"client-secret-value-for-tests"}
    for _, form, _ in oidc.provider.posts:
        secrets |= {form["code"], form["code_verifier"]}
    secrets.add(
        oidc.provider.id_token(next(iter(oidc.provider.grants.values())))
        if oidc.provider.grants
        else ""
    )
    secrets.discard("")
    recorded = json.dumps(oidc.world.events()) + caplog.text
    for secret in secrets:
        assert secret not in recorded


def test_provider_groups_are_read_again_after_group_refresh(oidc: Oidc) -> None:
    ana = linked(oidc)
    with_project(oidc, ana)
    session_id = oidc.sign_in_as("sub-ana", groups=["finance"]).session_id
    assert oidc.service.refresh_due(session_id) is None
    oidc.clock.move(minutes=59)
    assert oidc.service.refresh_due(session_id) is None
    oidc.clock.move(minutes=1)
    assert oidc.service.refresh_due(session_id) == "corp"
    started = oidc.service.start("corp", refreshing=session_id)
    assert "prompt=none" in started.location
    answer = oidc.provider.authorize(
        started.location, "sub-ana", groups=[]
    )  # removed at the provider
    renewed = oidc.service.callback("corp", answer, started.reference)
    assert groups(oidc, ana) == {"manual"}
    assert oidc.service.refresh_due(renewed.session_id) is None
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)  # the refreshed session was replaced


def test_a_failed_silent_reauthentication_ends_the_session(oidc: Oidc) -> None:
    linked(oidc)
    session_id = oidc.sign_in_as("sub-ana").session_id
    oidc.clock.move(hours=1)
    started = oidc.service.start("corp", refreshing=session_id)
    answer = {"error": "login_required", "state": "", "iss": oidc.provider.issuer}
    answer["state"] = oidc.provider.authorize(started.location, "sub-ana")["state"]
    assert (
        refused(oidc, lambda: oidc.service.callback("corp", answer, started.reference)) == "REFRESH"
    )
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)


def test_a_refresh_by_another_identity_ends_the_session(oidc: Oidc) -> None:
    linked(oidc)
    linked(oidc, "ben", "sub-ben")
    session_id = oidc.sign_in_as("sub-ana").session_id
    oidc.clock.move(hours=1)
    started = oidc.service.start("corp", refreshing=session_id)
    answer = oidc.provider.authorize(started.location, "sub-ben")
    assert (
        refused(oidc, lambda: oidc.service.callback("corp", answer, started.reference)) == "REFRESH"
    )
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)


def test_a_mapping_change_ends_the_sessions_of_the_users_it_affects(
    oidc: Oidc, world: World
) -> None:
    linked(oidc)
    session_id = oidc.sign_in_as("sub-ana").session_id
    changed = document()
    changed["mapping"] = changed["mapping"][:1]  # type: ignore[index]
    service = OidcSignIn(
        world.store, world.audit, parsed(changed, ENVIRONMENT), oidc.fetcher, oidc.clock
    )
    service.start_up()
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)
    changes = [e for e in world.events() if e["kind"] == "instance.oidc_mapping_changed"]
    assert changes[-1]["details"] == {"provider": "corp"}
    unchanged = OidcSignIn(
        world.store, world.audit, parsed(changed, ENVIRONMENT), oidc.fetcher, oidc.clock
    )
    count = len(world.events())
    unchanged.start_up()
    assert len(world.events()) == count  # the same mapping changes nothing


def test_a_password_sign_in_takes_back_every_provider_group(oidc: Oidc) -> None:
    """Provider groups never outlive `group_refresh` in a session that is not refreshed."""
    from orvex.access.authentication import credentials, passwords

    ana = linked(oidc)
    with_project(oidc, ana)
    oidc.sign_in_as("sub-ana", groups=["finance"])
    assert groups(oidc, ana) == {"finance", "manual"}
    with oidc.world.store.transaction() as transaction:
        credentials.store(
            transaction, ana, passwords.hashed("a local passphrase"), oidc.clock(), temporary=False
        )
    oidc.clock.move(hours=30)
    session_id = oidc.sign_in.login("ana", "a local passphrase", "192.0.2.1").session_id
    assert groups(oidc, ana) == {"manual"}  # hand-set groups stay
    assert oidc.service.refresh_due(session_id) is None
    assert oidc.sign_in.session(session_id).user.id == ana


def test_a_password_change_keeps_a_provider_session_refreshed(oidc: Oidc) -> None:
    from orvex.access.authentication import credentials, passwords

    ana = linked(oidc)
    with oidc.world.store.transaction() as transaction:
        credentials.store(
            transaction, ana, passwords.hashed("a local passphrase"), oidc.clock(), temporary=False
        )
    session_id = oidc.sign_in_as("sub-ana").session_id
    renewed = oidc.sign_in.change_password(
        session_id, "a local passphrase", "another passphrase", "192.0.2.1"
    )
    oidc.clock.move(hours=1)
    assert oidc.service.refresh_due(renewed) == "corp"


def test_a_removed_rule_takes_back_its_grants(oidc: Oidc, world: World) -> None:
    ana = linked(oidc)
    with_project(oidc, ana)
    oidc.sign_in_as("sub-ana", groups=["finance", "ops"])
    assert groups(oidc, ana) == {"finance", "ops", "manual"}
    changed = document()
    changed["mapping"] = changed["mapping"][1:]  # type: ignore[index]  # finance's rule is gone
    service = OidcSignIn(
        world.store, world.audit, parsed(changed, ENVIRONMENT), oidc.fetcher, oidc.clock
    )
    service.start_up()
    assert groups(oidc, ana) == {"ops", "manual"}
    without_ops = document()
    without_ops["mapping"] = []
    again = OidcSignIn(
        world.store, world.audit, parsed(without_ops, ENVIRONMENT), oidc.fetcher, oidc.clock
    )
    again.start_up()
    assert groups(oidc, ana) == {"manual"}


def test_a_hand_set_membership_in_a_mapped_group_is_never_taken(oidc: Oidc) -> None:
    ana = linked(oidc)
    with_project(oidc, ana)
    builder = next(e["actor"] for e in oidc.world.events() if e["kind"] == "group.created")
    oidc.world.directory.add_group_member(builder, "p1", "finance", ana)
    oidc.sign_in_as("sub-ana", groups=[])
    assert "finance" in groups(oidc, ana)


def test_an_unlink_between_the_checks_and_the_session_refuses_it(
    oidc: Oidc, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = linked(oidc)
    read = oidc.service._linked_user

    def then_unlinked(identity: object) -> object:
        user = read(identity)  # type: ignore[arg-type]
        oidc.identities.unlink(oidc.world.admin, ana, link_of(oidc, ana))
        return user

    monkeypatch.setattr(oidc.service, "_linked_user", then_unlinked)
    assert refused(oidc, lambda: oidc.sign_in_as("sub-ana")) == "UNLINKED"


@pytest.mark.parametrize(
    ("parameter", "reason"),
    [
        ("state", "STATE"),
        ("error", "ERROR_RESPONSE"),
        ("iss", "ISSUER"),
        ("code", "TOKEN_EXCHANGE"),
    ],
)
def test_an_overlong_parameter_is_refused_with_its_reason(
    oidc: Oidc, parameter: str, reason: str
) -> None:
    linked(oidc)
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    answer[parameter] = "x" * 4097
    assert refused(oidc, lambda: oidc.service.callback("corp", answer, started.reference)) == reason


def test_a_provider_that_fails_during_a_refresh_ends_the_session(oidc: Oidc, world: World) -> None:
    linked(oidc)
    session_id = oidc.sign_in_as("sub-ana").session_id
    oidc.clock.move(hours=1)
    started = oidc.service.start("corp", refreshing=session_id)
    answer = oidc.provider.authorize(started.location, "sub-ana")
    unreachable = OidcSignIn(
        world.store, world.audit, oidc.configuration, FakeFetcher(), oidc.clock
    )
    with pytest.raises(AccessError):
        unreachable.callback("corp", answer, started.reference)
    assert reason_of(oidc) == "REFRESH"
    with pytest.raises(AccessError):
        oidc.sign_in.session(session_id)


def test_one_issuer_is_one_provider() -> None:
    twice = document()
    twice["providers"]["again"] = dict(twice["providers"]["corp"])  # type: ignore[index]
    with pytest.raises(AccessError) as caught:
        parsed(twice, ENVIRONMENT)
    assert caught.value.details["setting"].endswith(".issuer")


def test_a_sign_in_through_one_provider_takes_back_anothers_grants(
    oidc: Oidc, world: World
) -> None:
    other = FakeProvider(issuer="https://other.example.com/realms/x", now=oidc.provider.now)
    both = document()
    both["providers"]["other"] = {  # type: ignore[index]
        "issuer": other.issuer,
        "client_id": "bi-client",
        "client_secret_variable": "CORP_SECRET",
        "algorithms": ["RS256"],
    }
    configuration = parsed(both, ENVIRONMENT)
    service = OidcSignIn(
        world.store, world.audit, configuration, FakeFetcher(oidc.provider, other), oidc.clock
    )
    service.start_up()
    ana = linked(oidc)
    Identities(world.store, world.audit, configuration).link(world.admin, ana, "other", "sub-x")
    with_project(oidc, ana)
    started = service.start("corp")
    service.callback(
        "corp",
        oidc.provider.authorize(started.location, "sub-ana", groups=["finance"]),
        started.reference,
    )
    assert "finance" in groups(oidc, ana)
    started = service.start("other")
    service.callback("other", other.authorize(started.location, "sub-x"), started.reference)
    assert groups(oidc, ana) == {"manual"}  # corp's grant is not refreshed by an `other` session


def test_a_builders_own_change_makes_a_membership_theirs(oidc: Oidc) -> None:
    ana = linked(oidc)
    with_project(oidc, ana)
    oidc.sign_in_as("sub-ana", groups=["finance"])
    builder = next(e["actor"] for e in oidc.world.events() if e["kind"] == "group.created")
    oidc.world.directory.remove_group_member(builder, "p1", "finance", ana)
    oidc.world.directory.add_group_member(builder, "p1", "finance", ana)
    oidc.sign_in_as("sub-ana", groups=[])
    assert "finance" in groups(oidc, ana)  # set by hand now: the mapping leaves it
