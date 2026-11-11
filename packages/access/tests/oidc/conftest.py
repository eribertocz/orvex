"""Fixtures for the OIDC tests (#439): the `oidc` world, with the public origin set."""

from __future__ import annotations

import pytest
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.oidc.configuration import parsed
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn

from tests.support.access_directory import World
from tests.support.oidc_provider import FakeFetcher, FakeProvider
from tests.support.oidc_world import ENVIRONMENT, ORIGIN, Oidc, document
from tests.support.sign_in import Clock


@pytest.fixture
def oidc(world: World) -> Oidc:
    clock = Clock()
    Settings(world.store, world.audit).change(world.admin, public_origin=ORIGIN)
    provider = FakeProvider(now=lambda: clock().timestamp())
    fetcher = FakeFetcher(provider)
    configuration = parsed(document(), ENVIRONMENT)
    service = OidcSignIn(world.store, world.audit, configuration, fetcher, clock)
    service.start_up()
    return Oidc(
        world,
        provider,
        fetcher,
        configuration,
        service,
        Identities(world.store, world.audit, configuration),
        SignIn(world.store, world.audit, clock),
        clock,
    )
