"""#440 AC7: the permission state of a next page is the one in force now: the current policies,
and the cursor's mapping snapshot only while nothing the policies read has changed since. When
it cannot be established, the next page is refused."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from orvex.contracts.store.v1 import ChangesRequest, ChangesResult
from orvex.semantic.errors import SemanticError
from orvex.server.permission_state import PermissionState, permission_state
from orvex.store.errors import StoreError

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


@dataclass
class Policies:
    fingerprint: str
    managed_reads: frozenset[str]


@dataclass
class Snapshot:
    id: int


@dataclass
class FakeLake:
    current: int
    changed: set[str] = field(default_factory=set)
    failing: bool = False
    asked: list[ChangesRequest] = field(default_factory=list)

    def current_snapshot(self) -> Snapshot:
        return Snapshot(self.current)

    def changes(self, request: ChangesRequest) -> ChangesResult:
        self.asked.append(request)
        if self.failing:
            raise StoreError("STORE_CATALOG_UNAVAILABLE", "The catalog is unavailable.", {})
        tables = [{"table": t.root, "changed": t.root in self.changed} for t in request.tables]
        return ChangesResult.model_validate(
            {
                "kind": "changes_result",
                "since": request.since,
                "snapshot": request.snapshot,
                "tables": tables,
            }
        )


def state(lake: FakeLake, at: int, policies: Policies, now: Policies | None) -> PermissionState:
    found: Any = lake
    return permission_state(found, at, policies, lambda _: now)  # type: ignore[arg-type,return-value]


MAPPED = Policies(OLD, frozenset({"security.user_zone"}))


def test_a_first_page_reads_the_current_state_and_asks_nothing() -> None:
    lake = FakeLake(current=7)
    assert state(lake, 7, MAPPED, None) == PermissionState(OLD, 7)
    assert lake.asked == []


def test_a_next_page_keeps_its_snapshot_while_nothing_it_depends_on_changed() -> None:
    lake = FakeLake(current=9, changed={"marts.readings"})
    assert state(lake, 7, MAPPED, MAPPED) == PermissionState(OLD, 7)
    (asked,) = lake.asked
    assert ([t.root for t in asked.tables], asked.since, asked.snapshot) == (
        ["security.user_zone"],
        7,
        9,
    )
    assert asked.count_removed is False  # the catalog alone: no row is read


def test_a_changed_mapping_moves_the_state_to_now() -> None:
    lake = FakeLake(current=9, changed={"security.user_zone"})
    assert state(lake, 7, MAPPED, MAPPED) == PermissionState(OLD, 9)


def test_a_table_only_the_current_policies_read_is_checked_too() -> None:
    lake = FakeLake(current=9, changed={"security.new_map"})
    now = Policies(OLD, frozenset({"security.new_map"}))
    assert state(lake, 7, Policies(OLD, frozenset()), now) == PermissionState(OLD, 9)


def test_changed_policies_move_the_state_to_now() -> None:
    lake = FakeLake(current=9)
    assert state(lake, 7, MAPPED, Policies(NEW, MAPPED.managed_reads)) == PermissionState(NEW, 9)


def test_policies_that_read_no_table_keep_the_cursors_snapshot() -> None:
    none = Policies(OLD, frozenset())
    lake = FakeLake(current=9)
    assert state(lake, 7, none, none) == PermissionState(OLD, 7)
    assert lake.asked == []


@pytest.mark.parametrize(
    ("lake", "now"),
    [(FakeLake(current=9), None), (FakeLake(current=9, failing=True), MAPPED)],
    ids=["no-model-now", "lake-fails"],
)
def test_permissions_that_cannot_be_read_refuse_the_next_page(
    lake: FakeLake, now: Policies | None
) -> None:
    with pytest.raises(SemanticError) as caught:
        state(lake, 7, MAPPED, now)
    assert caught.value.code == "PAGE_CURSOR_INVALID"
