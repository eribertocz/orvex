"""#446 AC6 and precisions 2 and 4: the directory's reads for the administration surfaces. Only
an administrator lists users; a user is found only by exact name, with minimal data, the same
not-found for every miss, and a budget against enumeration; a project's members, groups and
attributes are its Builder's; migration 7 gives every existing OIDC link an opaque id."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from orvex.access.directory import reads
from orvex.access.directory.reads import LOOKUP_BUDGET, DirectoryReads, Found, LookupLimited
from orvex.access.errors import ACCESS_FORBIDDEN, USER_NOT_FOUND, AccessError
from orvex.access.storage import PostgresStore, SqliteStore, migrate
from orvex.access.storage.postgres import migrations_for
from orvex.access.storage.sqlite import MIGRATIONS

from tests.support.access_directory import AnyStore, Backend, Raw, World


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def read(world: World, clock: Clock) -> DirectoryReads:
    return DirectoryReads(world.store, world.audit, clock)


@pytest.fixture
def project(world: World) -> dict[str, str]:
    """p1 with its Builder, ana (author) and vera (viewer); p2 with its own Builder."""
    people = {name: world.user(name) for name in ("builder", "ana", "vera", "other")}
    world.project_with_builder("p1", people["builder"])
    world.project_with_builder("p2", people["other"])
    world.directory.add_membership(people["builder"], "p1", people["ana"], "author")
    world.directory.add_membership(people["builder"], "p1", people["vera"], "viewer")
    return people


def code(action: object) -> str:
    with pytest.raises(AccessError) as caught:
        action()  # type: ignore[operator]
    return caught.value.code


# --- users ---------------------------------------------------------------------------------------


def test_an_administrator_lists_the_users_a_page_at_a_time(
    world: World, read: DirectoryReads, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("cid", "Ben", "ana"):
        world.user(name)
    monkeypatch.setattr(reads, "PAGE", 2)
    first = read.users(world.admin)
    assert [u.user_name for u in first.users] == ["ana", "Ben"]
    second = read.users(world.admin, first.after)
    assert [u.user_name for u in second.users] == ["cid", "root"]
    assert second.after is None
    assert read.user(world.admin, first.users[0].id) == first.users[0]
    assert code(lambda: read.user(world.admin, "u-nobody")) == USER_NOT_FOUND


def test_nobody_but_an_active_administrator_lists_users(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    for caller in (project["builder"], project["ana"], "u-nobody"):
        assert code(lambda caller=caller: read.users(caller)) == ACCESS_FORBIDDEN
        assert code(lambda caller=caller: read.user(caller, project["ana"])) == ACCESS_FORBIDDEN
    assert world.refusals()[-1] == ACCESS_FORBIDDEN


# --- finding a user by name ----------------------------------------------------------------------


def test_a_builder_finds_a_user_by_exact_name_with_minimal_data(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    newcomer = world.user("newcomer")
    for typed in ("newcomer", "NewComer"):
        assert read.find(project["other"], typed) == Found(newcomer, "Newcomer")
    assert {f.name for f in fields(Found)} == {"id", "display_name"}
    assert read.find(world.admin, "newcomer").id == newcomer


def test_every_miss_is_the_same_not_found(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    gone = world.user("gone")
    world.directory.deactivate_user(world.admin, gone)
    misses = ["newc", "an", "a*", "an%", "no-such-user", "", " ana", "gone", "x" * 300]
    found = []
    for typed in misses:
        with pytest.raises(AccessError) as caught:
            read.find(project["builder"], typed)
        found.append((caught.value.code, caught.value.message, dict(caught.value.details)))
    assert all(f == (USER_NOT_FOUND, "No such user.", {}) for f in found)
    events = world.events()[world.baseline :]
    assert not any(typed in str(e) for e in events for typed in ("newc", "no-such-user"))


def test_an_author_a_viewer_and_a_deactivated_builder_find_nobody(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    for caller in (project["ana"], project["vera"]):
        assert code(lambda caller=caller: read.find(caller, "builder")) == ACCESS_FORBIDDEN
    world.directory.add_membership(project["builder"], "p1", world.user("second"), "builder")
    world.directory.deactivate_user(world.admin, project["builder"])
    assert code(lambda: read.find(project["builder"], "ana")) == ACCESS_FORBIDDEN
    assert world.refusals().count(ACCESS_FORBIDDEN) == 3


def test_the_lookup_budget_refuses_any_name_then_comes_back(
    world: World, read: DirectoryReads, project: dict[str, str], clock: Clock
) -> None:
    builder = project["builder"]
    for i in range(LOOKUP_BUDGET):
        if i % 2:
            assert read.find(builder, "ana").id == project["ana"]
        else:  # an unknown name spends the budget too
            assert code(lambda i=i: read.find(builder, f"nobody{i}")) == USER_NOT_FOUND
    for name in ("ana", "nobody-at-all"):
        with pytest.raises(LookupLimited) as caught:
            read.find(builder, name)
        assert caught.value.retry_after == 60
    clock.now += timedelta(seconds=30)
    with pytest.raises(LookupLimited) as caught:
        read.find(builder, "ana")
    assert caught.value.retry_after == 30
    assert world.refusals()[-3:] == ["LOOKUP_LIMITED"] * 3
    assert read.find(project["other"], "ana").id == project["ana"]  # each caller their own
    clock.now += timedelta(seconds=30)
    assert read.find(builder, "ana").id == project["ana"]


# --- a project's members, groups and attributes ------------------------------------------------


def test_a_builder_reads_their_project_and_an_administrator_its_members_only(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    builder = project["builder"]
    world.directory.create_group(builder, "p1", "finance")
    world.directory.add_group_member(builder, "p1", "finance", project["ana"])
    members = read.members(builder, "p1")
    assert [(m.user.user_name, m.role) for m in members] == [
        ("ana", "author"),
        ("builder", "builder"),
        ("vera", "viewer"),
    ]
    assert read.members(world.admin, "p1") == members
    assert read.groups(builder, "p1") == {"finance": [project["ana"]]}
    assert read.attributes(builder, "p1", project["ana"]) == {}
    assert code(lambda: read.groups(world.admin, "p1")) == ACCESS_FORBIDDEN
    assert code(lambda: read.attributes(world.admin, "p1", project["ana"])) == ACCESS_FORBIDDEN


def test_nobody_reads_another_projects_members(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    other, ana, vera = project["other"], project["ana"], project["vera"]
    for action in (
        lambda: read.members(other, "p1"),
        lambda: read.groups(other, "p1"),
        lambda: read.members(ana, "p1"),
        lambda: read.groups(vera, "p1"),
    ):
        assert code(action) == ACCESS_FORBIDDEN
    builder = project["builder"]
    for user in (other, "u-nobody"):  # not a member, and no such user: the same
        assert code(lambda user=user: read.attributes(builder, "p1", user)) == USER_NOT_FOUND


# --- migration 7 ---------------------------------------------------------------------------------


def store_with(backend: Backend, migrations: tuple[tuple[str, ...], ...]) -> AnyStore:
    template = backend.store()
    made: AnyStore = (
        SqliteStore(template.path, migrations)
        if isinstance(template, SqliteStore)
        else PostgresStore(template.settings, migrations)
    )
    backend.opened.append(made)
    return made


@pytest.mark.parametrize("backend", ["sqlite", "postgres"], indirect=True)
def test_migration_7_gives_every_existing_link_a_unique_opaque_id(backend: Backend) -> None:
    current = MIGRATIONS if backend.kind == "sqlite" else migrations_for(str(backend.schema))
    older = store_with(backend, current[:6])
    assert migrate(older) == 6
    raw = Raw(backend, SimpleNamespace(store=older))  # type: ignore[arg-type]
    now = "2026-11-14T12:00:00.000000Z"
    raw.run(
        "INSERT INTO users VALUES ('u1', 'ana', 'Ana', 'active', FALSE, 1, :now, :now)", now=now
    )
    for subject in ("s1", "s2", "s3"):
        raw.run(
            "INSERT INTO oidc_identities VALUES ('https://id.example', :s, 'corp', 'u1', :now)",
            s=subject,
            now=now,
        )
    assert migrate(store_with(backend, current)) == 7
    ids = [str(r[0]) for r in raw.rows("SELECT link_id FROM oidc_identities")]
    raw.close()
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


def test_a_route_is_allowed_by_its_resources_read_rule(
    world: World, read: DirectoryReads, project: dict[str, str]
) -> None:
    cases = {
        ("administrator", None): {world.admin},
        ("finder", None): {world.admin, project["builder"], project["other"]},
        ("membership", "p1"): {world.admin, project["builder"]},
        ("builder", "p1"): {project["builder"]},
        ("anything else", "p1"): set(),
    }
    callers = {world.admin, *project.values()}
    for (permission, place), allowed in cases.items():
        found = {c for c in callers if read.allowed(c, permission, place)}
        assert found == allowed, permission
