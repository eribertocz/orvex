"""Fixtures for the sign-in tests (#438): the sign-in service and its settings on the world's
store, under a clock the test moves."""

from __future__ import annotations

import pytest
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings

from tests.support.access_directory import World
from tests.support.sign_in import Accounts, Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sign_in(world: World, clock: Clock) -> SignIn:
    return SignIn(world.store, world.audit, clock)


@pytest.fixture
def settings(world: World, clock: Clock) -> Settings:
    return Settings(world.store, world.audit, clock)


@pytest.fixture
def accounts(world: World, sign_in: SignIn, clock: Clock) -> Accounts:
    return Accounts(world, sign_in, clock)
