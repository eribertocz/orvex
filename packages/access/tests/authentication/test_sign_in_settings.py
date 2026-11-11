"""#438 AC7: the sign-in settings are the administrator's only, validated, and audited by name."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network

import pytest
from orvex.access.authentication import settings as sign_in_settings
from orvex.access.authentication.settings import Settings
from orvex.access.errors import ACCESS_FORBIDDEN, VALIDATION_FAILED, AccessError

from tests.support.access_directory import World


def test_the_defaults_are_twelve_hours_one_hour_no_origin_no_proxies(settings: Settings) -> None:
    current = settings.current()
    assert (current.absolute, current.idle) == (timedelta(hours=12), timedelta(hours=1))
    assert current.public_origin is None
    assert current.trusted_proxies == ()


def test_an_administrator_changes_them_and_the_log_names_what_changed(
    settings: Settings, world: World
) -> None:
    changed = settings.change(
        world.admin,
        idle=timedelta(minutes=30),
        public_origin="https://BI.Example.com:443",
        trusted_proxies=["10.0.0.0/8", "2001:db8::/32", "10.0.0.0/8"],
    )
    assert changed == settings.current()
    assert changed.public_origin == "https://bi.example.com"
    assert changed.trusted_proxies == (ip_network("10.0.0.0/8"), ip_network("2001:db8::/32"))
    event = world.events()[-1]
    assert event["kind"] == "instance.settings_changed"
    assert event["details"] == {"settings": ["idle_lifetime", "public_origin", "trusted_proxies"]}
    count = len(world.events())
    settings.change(world.admin, idle=timedelta(minutes=30))  # no change, no event
    assert len(world.events()) == count


def test_a_user_who_is_not_an_administrator_changes_nothing(
    settings: Settings, world: World
) -> None:
    ana = world.user("ana")
    with pytest.raises(AccessError) as caught:
        settings.change(ana, public_origin="https://evil.example")
    assert caught.value.code == ACCESS_FORBIDDEN
    assert settings.current().public_origin is None
    assert world.refusals() == [ACCESS_FORBIDDEN]


@pytest.mark.parametrize(
    "origin",
    [
        "http://bi.example.com",
        "https://bi.example.com/",
        "https://bi.example.com/path",
        "https://user@bi.example.com",
        "https://bi.example.com?x=1",
        "https://bi.example.com#x",
        "https://bi.example.com:0",
        "https://bi.example.com:99999",
        " https://bi.example.com",
        "https://",
        "https://bï.example.com",
        "https://" + "a" * 300,
    ],
)
def test_a_public_origin_is_exactly_scheme_host_and_port(origin: str) -> None:
    with pytest.raises(AccessError) as caught:
        sign_in_settings.checked_origin(origin)
    assert caught.value.code == VALIDATION_FAILED


def test_an_origin_keeps_a_port_that_is_not_the_default() -> None:
    assert sign_in_settings.checked_origin("https://bi.example.com:8443") == (
        "https://bi.example.com:8443"
    )
    assert sign_in_settings.checked_origin("https://[2001:DB8::1]") == "https://[2001:db8::1]"


@pytest.mark.parametrize(
    ("absolute", "idle", "field"),
    [
        (timedelta(minutes=14), timedelta(minutes=5), "absolute_lifetime"),
        (timedelta(days=31), timedelta(hours=1), "absolute_lifetime"),
        (timedelta(hours=1), timedelta(minutes=4), "idle_lifetime"),
        (timedelta(hours=1), timedelta(hours=2), "idle_lifetime"),
        (timedelta(hours=1, microseconds=1), timedelta(minutes=5), "absolute_lifetime"),
    ],
)
def test_lifetimes_stay_within_their_bounds(
    absolute: timedelta, idle: timedelta, field: str
) -> None:
    with pytest.raises(AccessError) as caught:
        sign_in_settings.checked_lifetimes(absolute, idle)
    assert caught.value.details == {"field": field}


@pytest.mark.parametrize(
    "proxies", [["10.0.0.1/8"], ["not a network"], ["10.0.0.1"] * 65, "10.0.0.0/8"]
)
def test_trusted_proxies_are_networks(proxies: list[str]) -> None:
    with pytest.raises(AccessError) as caught:
        sign_in_settings.checked_proxies(proxies)
    assert caught.value.details == {"field": "trusted_proxies"}
