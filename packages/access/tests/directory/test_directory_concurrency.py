"""#436 AC8: concurrent changes never leave a project without an active Builder, or the
instance without an active administrator, on SQLite (two threads) and PostgreSQL (two
connections), each race repeated many times."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from orvex.access.errors import (
    ACCESS_FORBIDDEN,
    LAST_BUILDER,
    AccessError,
)

from tests.support.access_directory import World

ROUNDS = 200


class Racer:
    """Two threads that live for the whole test, so each keeps its one store connection, and
    that start each pair of changes at the same moment."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=2)

    def race(self, first: Callable[[], None], second: Callable[[], None]) -> list[str]:
        """Run both at once; return each one's outcome: `done` or its refusal code."""
        start = threading.Barrier(2)

        def outcome(work: Callable[[], None]) -> str:
            start.wait(timeout=30)
            try:
                work()
            except AccessError as error:
                return error.code
            return "done"

        futures = [self._pool.submit(outcome, work) for work in (first, second)]
        return [future.result(timeout=60) for future in futures]

    def close(self) -> None:
        self._pool.shutdown()


@pytest.fixture
def racer() -> Iterator[Racer]:
    made = Racer()
    yield made
    made.close()


def test_two_deactivations_of_the_last_two_builders(world: World, racer: Racer) -> None:
    other_admin = world.user("other-admin")
    world.directory.set_administrator(world.admin, other_admin, True)
    directory = world.directory
    for round in range(ROUNDS):
        project = f"p{round}"
        first, second = world.user(f"a{round}"), world.user(f"b{round}")
        world.project_with_builder(project, first)
        directory.add_membership(first, project, second, "builder")
        outcomes = racer.race(
            partial(directory.deactivate_user, world.admin, first),
            partial(directory.deactivate_user, other_admin, second),
        )
        assert sorted(outcomes) == sorted(["done", LAST_BUILDER]), (round, outcomes)
        active = [user for user in (first, second) if directory.role_of(user, project) == "builder"]
        assert len(active) == 1, round


def test_two_builders_removing_each_other(world: World, racer: Racer) -> None:
    directory = world.directory
    for round in range(ROUNDS):
        project = f"q{round}"
        first, second = world.user(f"c{round}"), world.user(f"d{round}")
        world.project_with_builder(project, first)
        directory.add_membership(first, project, second, "builder")
        outcomes = racer.race(
            partial(directory.remove_membership, first, project, second),
            partial(directory.remove_membership, second, project, first),
        )
        # The one that runs second is no longer a member: it may do nothing.
        assert sorted(outcomes) == sorted(["done", ACCESS_FORBIDDEN]), (round, outcomes)
        builders = [u for u in (first, second) if directory.role_of(u, project) == "builder"]
        assert len(builders) == 1, round


def test_two_administrators_removing_each_other(world: World, racer: Racer) -> None:
    directory = world.directory
    other = world.user("other")
    directory.set_administrator(world.admin, other, True)
    for round in range(ROUNDS // 4):
        outcomes = racer.race(
            partial(directory.set_administrator, world.admin, other, False),
            partial(directory.set_administrator, other, world.admin, False),
        )
        # The one that runs second is no longer an administrator: it may do nothing.
        assert sorted(outcomes) == sorted(["done", ACCESS_FORBIDDEN]), (round, outcomes)
        admins = [
            u for u in (world.admin, other) if (found := directory.user(u)) and found.administrator
        ]
        assert len(admins) == 1, round
        (remaining,) = admins
        removed = other if remaining == world.admin else world.admin
        directory.set_administrator(remaining, removed, True)
