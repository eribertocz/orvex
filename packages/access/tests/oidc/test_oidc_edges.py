"""#439: the malformed inputs each OIDC step refuses: configuration documents of the wrong shape,
discovery and key documents that are not what they claim, tokens that cannot be read, and the
administrator's link operations given what does not exist."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from orvex.access.errors import OIDC_FAILED, OIDC_NOT_CONFIGURED, VALIDATION_FAILED, AccessError
from orvex.access.oidc import tokens
from orvex.access.oidc.configuration import Provider, parsed
from orvex.access.oidc.discovery import Keys, checked_metadata

from tests.support.oidc_provider import FakeFetcher, FakeProvider
from tests.support.oidc_world import ENVIRONMENT, Oidc, document


def provider() -> Provider:
    return parsed(document(), ENVIRONMENT).provider("corp")


@pytest.mark.parametrize(
    "broken",
    [
        {"extra": 1},
        {"providers": []},
        {"mapping": "x"},
        {"providers": {"Bad Name": {}}},
        {"providers": {"corp": "not a table"}},
    ],
)
def test_a_document_of_the_wrong_shape_is_refused(broken: dict[str, object]) -> None:
    with pytest.raises(AccessError) as caught:
        parsed(broken, ENVIRONMENT)
    assert caught.value.code == OIDC_NOT_CONFIGURED


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": ""},
        {"client_id": 7},
        {"internal": "yes"},
        {"algorithms": "RS256"},
        {"group_refresh_minutes": True},
        {"group_refresh_minutes": "60"},
        {"issuer": 7},
        {"issuer": "https://idp.example.com:99999/x"},
        {"issuer": "https://idp.example.com/a b"},
        {"internal": True, "internal_ranges": ["10.0.0.1/8"]},
        {"client_secret_variable": "lower"},
    ],
)
def test_a_provider_value_of_the_wrong_type_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(AccessError) as caught:
        parsed(document(**change), ENVIRONMENT)
    assert caught.value.code == OIDC_NOT_CONFIGURED


@pytest.mark.parametrize(
    "rule",
    [
        {"provider": "corp", "claim": "groups", "value": "", "project": "p1", "group": "g"},
        {"provider": "corp", "claim": "groups", "value": "v", "project": "bad id!", "group": "g"},
        {"provider": "corp", "claim": "bad claim", "value": "v", "project": "p1", "group": "g"},
        "not a table",
    ],
)
def test_a_mapping_rule_of_the_wrong_shape_is_refused(rule: object) -> None:
    changed = document()
    changed["mapping"] = [rule]
    with pytest.raises(AccessError) as caught:
        parsed(changed, ENVIRONMENT)
    assert caught.value.code == OIDC_NOT_CONFIGURED


@pytest.mark.parametrize(
    "answer",
    [
        b"not json",
        b"[]",
        json.dumps({"issuer": "https://idp.example.com/realms/corp"}).encode(),
        json.dumps(
            {
                **FakeProvider().discovery(),
                "authorization_endpoint": "http://idp.example.com/auth",
            }
        ).encode(),
        json.dumps({**FakeProvider().discovery(), "token_endpoint": "https://[x/"}).encode(),
        json.dumps(
            {**FakeProvider().discovery(), "code_challenge_methods_supported": "S256"}
        ).encode(),
    ],
)
def test_a_discovery_document_that_is_not_one_is_refused_after_start(answer: bytes) -> None:
    with pytest.raises(AccessError) as caught:
        checked_metadata(provider(), answer, at_start=False)
    assert caught.value.code == OIDC_FAILED


class KeysAnswer(FakeFetcher):
    def __init__(self, answer: bytes) -> None:
        super().__init__(FakeProvider())
        self.answer = answer

    def get(self, provider: Provider, url: str) -> bytes:
        return self.answer if url.endswith("/certs") else super().get(provider, url)


@pytest.mark.parametrize(
    "answer", [b"not json", b"[]", b'{"keys": "x"}', b'{"keys": [{"kty": "nope"}]}']
)
def test_a_key_document_that_is_not_one_is_refused_and_not_read_again_at_once(
    answer: bytes,
) -> None:
    fake = FakeProvider()
    metadata = checked_metadata(provider(), json.dumps(fake.discovery()).encode(), at_start=True)
    keys = Keys(provider(), metadata, KeysAnswer(answer))
    with pytest.raises(AccessError):
        keys.current("r1")
    with pytest.raises(AccessError):
        keys.current("r1")  # the failed read is not repeated within the minute
    assert keys.reads == 1


def test_private_keys_and_encryption_keys_are_never_used(oidc: Oidc) -> None:
    fake = oidc.provider
    private = fake.rsa.as_dict(private=True)
    encrypting = {**fake.ec.as_dict(private=False), "use": "enc"}
    answer = json.dumps({"keys": [private, encrypting]}).encode()
    metadata = checked_metadata(provider(), json.dumps(fake.discovery()).encode(), at_start=True)
    with pytest.raises(AccessError):  # nothing usable is left: no key set at all
        Keys(provider(), metadata, KeysAnswer(answer)).current(None)


def _token(header: object, body: object = None) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(body or {})}.c2ln"


@pytest.mark.parametrize(
    "token",
    [
        "x" * (64 * 1024 + 1),
        "not.a.token",
        _token({"alg": "RS256", "kid": 7}),
        _token({"alg": "ES512"}),
        "é.a.b",
    ],
)
def test_a_token_that_cannot_be_read_is_a_signature_failure(oidc: Oidc, token: str) -> None:
    keys = oidc.service._provider("corp")[1].keys
    with pytest.raises(AccessError) as caught:
        tokens.validated(provider(), keys, token, "n", datetime.now(UTC))
    assert caught.value.details["reason"] == "signature"


@pytest.mark.parametrize(
    "claims",
    [{"exp": "soon"}, {"exp": True}, {"exp": 10**20}, {"iat": None, "exp": None}],
)
def test_a_time_claim_of_the_wrong_type_is_refused(oidc: Oidc, claims: dict[str, object]) -> None:
    oidc.world.directory.create_user(oidc.world.admin, "ana", "Ana")
    oidc.provider.claims_extra = claims
    started = oidc.service.start("corp")
    answer = oidc.provider.authorize(started.location, "sub-ana")
    with pytest.raises(AccessError) as caught:
        oidc.service.callback("corp", answer, started.reference)
    assert caught.value.code == OIDC_FAILED


def test_the_link_operations_refuse_what_does_not_exist(oidc: Oidc) -> None:
    ana = oidc.world.user("ana")
    admin = oidc.world.admin
    for action in (
        lambda: oidc.identities.link(admin, ana, "corp", ""),
        lambda: oidc.identities.link(admin, "u-nobody", "corp", "s"),
        lambda: oidc.identities.unlink(admin, ana, "corp", "never-linked"),
    ):
        with pytest.raises(AccessError) as caught:
            action()
        assert caught.value.code == VALIDATION_FAILED
    oidc.identities.link(admin, ana, "corp", "s")
    with pytest.raises(AccessError) as caught:
        oidc.identities.link(admin, oidc.world.user("ben"), "corp", "s")  # already linked
    assert caught.value.details == {"field": "identity"}


def test_a_session_from_a_provider_no_longer_configured_is_always_due(oidc: Oidc) -> None:
    from orvex.access.oidc.service import OidcSignIn

    oidc.identities.link(oidc.world.admin, oidc.world.user("ana"), "corp", "sub-ana")
    session_id = oidc.sign_in_as("sub-ana").session_id
    empty = parsed({}, ENVIRONMENT)
    without = OidcSignIn(oidc.world.store, oidc.world.audit, empty, oidc.fetcher, oidc.clock)
    assert without.refresh_due(session_id) == "corp"
    oidc.clock.move(minutes=1)
    assert oidc.service.refresh_due(session_id) is None
    assert oidc.service.refresh_due("unknown") is None
    assert timedelta(hours=1) == oidc.configuration.provider("corp").group_refresh
