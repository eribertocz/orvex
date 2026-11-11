"""#439 AC7, R2, R5: a configuration is refused at start, naming the setting, never a secret."""

from __future__ import annotations

import pytest
from orvex.access.errors import OIDC_NOT_CONFIGURED, AccessError
from orvex.access.oidc.configuration import parsed
from orvex.access.oidc.service import OidcSignIn

from tests.support.access_directory import World
from tests.support.oidc_provider import SECRET, FakeFetcher, FakeProvider
from tests.support.oidc_world import ENVIRONMENT, document


def setting(document_: dict[str, object], environment: dict[str, str] = ENVIRONMENT) -> str:
    with pytest.raises(AccessError) as caught:
        parsed(document_, environment)
    assert caught.value.code == OIDC_NOT_CONFIGURED
    assert SECRET not in str(caught.value) + repr(caught.value.details)
    return str(caught.value.details["setting"])


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"issuer": "http://idp.example.com/realms/corp"}, "provider.corp.issuer"),
        ({"issuer": "https://user@idp.example.com/x"}, "provider.corp.issuer"),
        ({"issuer": "https://idp.example.com/x?y=1"}, "provider.corp.issuer"),
        ({"algorithms": ["RS256", "none"]}, "provider.corp.algorithms"),
        ({"algorithms": ["HS256"]}, "provider.corp.algorithms"),
        ({"algorithms": []}, "provider.corp.algorithms"),
        ({"client_secret_variable": "MISSING_SECRET"}, "provider.corp.client_secret_variable"),
        ({"group_refresh_minutes": 24 * 60 + 1}, "provider.corp.group_refresh_minutes"),
        ({"group_refresh_minutes": 4}, "provider.corp.group_refresh_minutes"),
        ({"internal_ranges": ["10.0.0.0/8"]}, "provider.corp.internal_ranges"),
        ({"internal": True}, "provider.corp.internal_ranges"),
        ({"internal": True, "internal_ranges": ["127.0.0.0/8"]}, "provider.corp.internal_ranges"),
        (
            {"internal": True, "internal_ranges": ["169.254.0.0/16"]},
            "provider.corp.internal_ranges",
        ),
        ({"scopes": ["openid"]}, "provider.corp.scopes"),
        ({"endpoint_hosts": ["Bad Host"]}, "provider.corp.endpoint_hosts"),
        ({"unknown": 1}, "provider.corp.unknown"),
    ],
)
def test_a_provider_setting_is_refused_by_name(change: dict[str, object], expected: str) -> None:
    changed = document(**change)
    assert setting(changed) == expected


def test_a_mapping_rule_is_refused_by_name() -> None:
    changed = document()
    changed["mapping"] = [
        {"provider": "nobody", "claim": "g", "value": "v", "project": "p1", "group": "g"}
    ]
    assert setting(changed) == "mapping.0.provider"
    changed["mapping"] = [{"provider": "corp", "claim": "g", "value": "v", "project": "p1"}]
    assert setting(changed) == "mapping.0"


def test_an_internal_provider_with_its_range_is_accepted() -> None:
    found = parsed(document(internal=True, internal_ranges=["10.20.0.0/16"]), ENVIRONMENT)
    assert found.provider("corp").internal
    assert SECRET not in repr(found.provider("corp"))


def test_a_provider_that_is_not_configured_is_refused() -> None:
    with pytest.raises(AccessError) as caught:
        parsed(document(), ENVIRONMENT).provider("other")
    assert caught.value.code == OIDC_NOT_CONFIGURED


@pytest.mark.parametrize(
    ("discovery", "expected"),
    [
        ({"issuer": "https://idp.example.com/realms/corp/"}, "provider.corp.issuer"),
        ({"token_endpoint": "https://elsewhere.example/token"}, "provider.corp.endpoint_hosts"),
        ({"jwks_uri": "http://idp.example.com/certs"}, "provider.corp.endpoint_hosts"),
        ({"code_challenge_methods_supported": ["plain"]}, "provider.corp.issuer"),
    ],
)
def test_a_discovery_document_is_checked_at_start(
    world: World, discovery: dict[str, object], expected: str
) -> None:
    provider = FakeProvider(discovery_extra=discovery)
    service = OidcSignIn(
        world.store, world.audit, parsed(document(), ENVIRONMENT), FakeFetcher(provider)
    )
    with pytest.raises(AccessError) as caught:
        service.start_up()
    assert caught.value.code == OIDC_NOT_CONFIGURED
    assert caught.value.details["setting"] == expected


def test_an_endpoint_on_a_listed_host_is_accepted(world: World) -> None:
    provider = FakeProvider(discovery_extra={"token_endpoint": "https://tokens.example.com/t"})
    configuration = parsed(document(endpoint_hosts=["tokens.example.com"]), ENVIRONMENT)
    OidcSignIn(world.store, world.audit, configuration, FakeFetcher(provider)).start_up()


def test_an_unreachable_provider_is_refused_at_start(world: World) -> None:
    service = OidcSignIn(world.store, world.audit, parsed(document(), ENVIRONMENT), FakeFetcher())
    with pytest.raises(AccessError) as caught:
        service.start_up()
    assert caught.value.details["setting"] == "provider.corp.issuer"
