"""#445 AC3: the per-role query limits start at the approved defaults, stay within their bounds,
are changed only by an instance administrator, and every change is audited by name, never by
value. There is no limit on rows."""

from __future__ import annotations

import pytest
from orvex.access.authentication.query_limits import DEFAULT_LIMITS, QueryLimits, RoleLimit
from orvex.access.errors import ACCESS_FORBIDDEN, VALIDATION_FAILED, AccessError

from tests.support.access_directory import World


@pytest.fixture
def limits(world: World) -> QueryLimits:
    return QueryLimits(world.store, world.audit)


def test_the_store_starts_with_the_approved_defaults(limits: QueryLimits) -> None:
    assert limits.current() == DEFAULT_LIMITS
    assert {
        "builder": RoleLimit(4, 120_000),
        "author": RoleLimit(2, 60_000),
        "viewer": RoleLimit(2, 30_000),
    } == DEFAULT_LIMITS
    assert set(RoleLimit.__slots__) == {"max_concurrent", "max_run_ms"}  # never a row limit


def test_an_administrator_changes_one_role_and_the_log_names_it(
    limits: QueryLimits, world: World
) -> None:
    changed = limits.change(world.admin, {"viewer": RoleLimit(1, 10_000)})
    assert changed == limits.current()
    assert limits.current()["viewer"] == RoleLimit(1, 10_000)
    assert limits.current()["author"] == DEFAULT_LIMITS["author"]
    event = world.events()[-1]
    assert (event["kind"], event["actor"]) == ("instance.settings_changed", world.admin)
    assert event["details"] == {"settings": ["query_limits.viewer"]}
    count = len(world.events())
    limits.change(world.admin, {"viewer": RoleLimit(1, 10_000)})  # no change, no event
    assert len(world.events()) == count


def test_someone_who_is_not_an_administrator_changes_nothing(
    limits: QueryLimits, world: World
) -> None:
    ana = world.user("ana")
    with pytest.raises(AccessError) as caught:
        limits.change(ana, {"builder": RoleLimit(64, 3_600_000)})
    assert caught.value.code == ACCESS_FORBIDDEN
    assert limits.current() == DEFAULT_LIMITS


@pytest.mark.parametrize(
    ("role", "limit", "field"),
    [
        ("viewer", RoleLimit(0, 30_000), "max_concurrent"),
        ("viewer", RoleLimit(65, 30_000), "max_concurrent"),
        ("viewer", RoleLimit(2, 999), "max_run_ms"),
        ("viewer", RoleLimit(2, 3_600_001), "max_run_ms"),
        ("admin", RoleLimit(2, 30_000), "role"),
    ],
)
def test_a_value_out_of_bounds_changes_nothing(
    limits: QueryLimits, world: World, role: str, limit: RoleLimit, field: str
) -> None:
    with pytest.raises(AccessError) as caught:
        limits.change(world.admin, {role: limit})
    assert (caught.value.code, caught.value.details) == (VALIDATION_FAILED, {"field": field})
    assert limits.current() == DEFAULT_LIMITS


def test_the_bounds_themselves_are_accepted(limits: QueryLimits, world: World) -> None:
    limits.change(world.admin, {"builder": RoleLimit(64, 3_600_000), "viewer": RoleLimit(1, 1_000)})
    assert limits.current()["builder"] == RoleLimit(64, 3_600_000)
    assert limits.current()["viewer"] == RoleLimit(1, 1_000)
