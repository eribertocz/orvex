"""#446 over HTTP in server mode: the administration routes call the domain and nothing else
decides (AC1); users (AC2), memberships (AC3), groups and attributes (AC4), reads and the
lookup (AC6), the request rules and the served projects (AC7), temporary passwords (AC8)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from orvex.access.directory import reads

from tests.support.access_directory import audit_events
from tests.support.semantic_http import detail

from .test_server_semantic import ORIGIN, SAME, Server, ask

A = "/api/v1/admin"
P1 = f"{A}/projects/project-1"


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path, administration=True)


def code(answer: Any) -> tuple[int, str]:  # noqa: ANN401
    return answer.status_code, answer.json()["error"]["code"]


def events(server: Server, kind: str) -> list[dict[str, Any]]:
    return [e for e in audit_events(server.store) if e["kind"] == kind]


def refusals(server: Server) -> int:
    return len(events(server, "access.refused"))


# --- AC1: who may do what, decided by the domain -------------------------------------------------


@pytest.fixture
def people(server: Server) -> dict[str, str]:
    """root (administrator), builder (p1), other (p2's Builder), ana (author), vera (viewer)."""
    found = {"root": server.admin, "builder": server.builder}
    found["other"] = server.person("other")
    server.directory.add_membership(server.admin, "project-2", found["other"], "builder")
    found["ana"] = server.author("ana")
    found["vera"] = server.viewer("vera")
    found["target"] = server.person("target")
    return found


WRITES = [
    ("POST", f"{A}/users", {"user_name": "newbie", "display_name": "Newbie"}, {"root"}),
    ("POST", f"{A}/users/{{target}}/deactivate", {}, {"root"}),
    ("PUT", f"{A}/users/{{target}}/administrator", {"value": True}, {"root"}),
    ("POST", f"{A}/users/{{target}}/password-reset", {}, {"root"}),
    ("PUT", f"{P1}/members/{{target}}", {"role": "viewer"}, {"builder"}),
    ("POST", f"{P1}/groups", {"name": "new-group"}, {"builder"}),
    ("PUT", f"{P1}/groups/finance/members/{{ana}}", {}, {"builder"}),
    ("PUT", f"{P1}/members/{{ana}}/attributes/region", {"type": "text", "value": "n"}, {"builder"}),
]


@pytest.mark.parametrize(("method", "path", "body", "allowed"), WRITES)
def test_each_write_is_allowed_exactly_to_whom_the_domain_allows(
    server: Server,
    people: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any],
    allowed: set[str],
) -> None:
    concrete = path.format(**people)
    for name in ("vera", "ana", "other", "builder", "root"):
        if name in allowed:
            continue
        before = refusals(server)
        answer = server.client(name).request(method, concrete, json=body, headers=SAME)
        assert code(answer) == (403, "ACCESS_FORBIDDEN"), (name, answer.text)
        assert refusals(server) == before + 1  # audited, nothing changed
    for name in allowed:
        answer = server.client(name).request(method, concrete, json=body, headers=SAME)
        assert answer.status_code in {200, 201, 204}, (name, answer.text)


def test_nobody_changes_their_own_permissions(server: Server, people: dict[str, str]) -> None:
    root = server.client("root")
    assert code(
        root.put(f"{A}/users/{server.admin}/administrator", json={"value": False}, headers=SAME)
    ) == (409, "LAST_ADMINISTRATOR")
    builder = server.client("builder")
    me = server.builder
    assert code(builder.put(f"{P1}/members/{me}/attributes/x", json={"type": "text", "value": "y"},
                            headers=SAME)) == (403, "ACCESS_FORBIDDEN")  # fmt: skip
    assert code(builder.put(f"{P1}/groups/finance/members/{me}", json={}, headers=SAME)) == (
        403,
        "ACCESS_FORBIDDEN",
    )


# --- AC2: users ----------------------------------------------------------------------------------


def test_an_administrator_manages_a_user_end_to_end(server: Server, caplog: Any) -> None:  # noqa: ANN401
    caplog.set_level(logging.DEBUG)
    root = server.client("root")
    made = root.post(f"{A}/users", json={"user_name": "Cid", "display_name": "Cid"}, headers=SAME)
    assert made.status_code == 201, made.text
    assert made.headers["Cache-Control"] == "no-store"
    cid, temporary = made.json()["user"]["id"], made.json()["temporary_password"]
    assert set(made.json()["user"]) == {
        "id", "user_name", "display_name", "state", "administrator", "created_at", "changed_at"
    }  # fmt: skip
    taken = root.post(f"{A}/users", json={"user_name": "cid", "display_name": "X"}, headers=SAME)
    assert code(taken) == (409, "USER_NAME_TAKEN")
    bad = root.post(f"{A}/users", json={"user_name": " x", "display_name": "X"}, headers=SAME)
    assert code(bad) == (422, "VALIDATION_FAILED")
    http = TestClient(server.app, base_url=ORIGIN)
    signed = http.post("/api/v1/auth/login", json={"user_name": "cid", "password": temporary},
                       headers=SAME)  # fmt: skip
    assert signed.json()["password_change_required"] is True
    assert root.post(f"{A}/users/{cid}/deactivate", json={}, headers=SAME).status_code == 204
    assert code(http.get("/api/v1/auth/session")) == (401, "SESSION_EXPIRED")
    assert root.get(f"{A}/users/{cid}").json()["state"] == "deactivated"
    assert root.post(f"{A}/users/{cid}/reactivate", json={}, headers=SAME).status_code == 204
    reset = root.post(f"{A}/users/{cid}/password-reset", json={}, headers=SAME)
    assert reset.headers["Cache-Control"] == "no-store"
    second = reset.json()["temporary_password"]
    assert root.put(f"{A}/users/{cid}/administrator", json={"value": True}, headers=SAME
                    ).status_code == 204  # fmt: skip
    assert root.get(f"{A}/users/{cid}").json()["administrator"] is True
    assert code(root.get(f"{A}/users/u-nobody")) == (404, "USER_NOT_FOUND")
    assert code(root.post(f"{A}/users/u-nobody/deactivate", json={}, headers=SAME)) == (
        404,
        "USER_NOT_FOUND",
    )
    kinds = [e["kind"] for e in audit_events(server.store)]
    for kind in ("user.created", "user.deactivated", "user.reactivated", "user.password_reset",
                 "user.administrator_granted"):  # fmt: skip
        assert kind in kinds
    everything = caplog.text + str(audit_events(server.store))
    assert temporary not in everything
    assert second not in everything


def test_the_user_list_pages_and_shows_no_credential(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reads, "PAGE", 1)
    root = server.client("root")
    first = root.get(f"{A}/users").json()
    assert [u["user_name"] for u in first["users"]] == ["builder"]
    second = root.get(f"{A}/users", params={"after": first["after"]}).json()
    assert [u["user_name"] for u in second["users"]] == ["root"]
    assert second["after"] is None
    text = str(first) + str(second)
    for secret in ("authz_version", "password", "session", "hash"):
        assert secret not in text


# --- AC3: memberships ----------------------------------------------------------------------------


def test_the_first_builder_is_the_administrators_then_the_builders(server: Server) -> None:
    root = server.client("root")
    first = server.person("first")
    late = server.person("late")
    put = f"{A}/projects/project-2/members"
    assert root.put(f"{put}/{first}", json={"role": "builder"}, headers=SAME).status_code == 204
    assert code(root.put(f"{put}/{late}", json={"role": "viewer"}, headers=SAME)) == (
        403,
        "ACCESS_FORBIDDEN",
    )
    builder = server.client("first")
    assert builder.put(f"{put}/{late}", json={"role": "viewer"}, headers=SAME).status_code == 204
    assert builder.put(f"{put}/{late}", json={"role": "author"}, headers=SAME).status_code == 204
    roles = {m["user_name"]: m["role"] for m in builder.get(put).json()["members"]}
    assert roles == {"first": "builder", "late": "author"}
    assert root.get(put).json()["members"] == builder.get(put).json()["members"]
    count = len(events(server, "membership.changed"))
    assert builder.put(f"{put}/{late}", json={"role": "author"}, headers=SAME).status_code == 204
    assert len(events(server, "membership.changed")) == count  # nothing changed: no event
    assert code(builder.delete(f"{put}/{first}", headers=SAME)) == (409, "LAST_BUILDER")
    assert builder.delete(f"{put}/{late}", headers=SAME).status_code == 204
    assert code(builder.delete(f"{put}/{late}", headers=SAME)) == (404, "USER_NOT_FOUND")


# --- AC4: groups and attributes, effective at the next request ---------------------------------


def test_a_group_and_an_attribute_change_the_next_answer(server: Server) -> None:
    ana = server.author("ana", tariff=None)
    builder = server.client("builder")
    http = server.client("ana")
    assert ask(http, detail(10)).json()["result"]["rows"] == []  # no tariff: zero rows
    assert builder.put(f"{P1}/members/{ana}/attributes/tariff",
                       json={"type": "text", "value": "residential"}, headers=SAME
                       ).status_code == 204  # fmt: skip
    assert [r[0] for r in ask(http, detail(10)).json()["result"]["rows"]] == [1, 2]
    listed = builder.get(f"{P1}/members/{ana}/attributes").json()
    assert listed == {"attributes": [{"name": "tariff", "type": "text", "value": "residential"}]}
    assert builder.put(f"{P1}/groups/finance/members/{ana}", json={}, headers=SAME
                       ).status_code == 204  # fmt: skip
    assert code(http.get("/api/v1/auth/session")) == (401, "SESSION_EXPIRED")  # groups: bumped
    groups = builder.get(f"{P1}/groups").json()["groups"]
    assert {"name": "finance", "members": [ana]} in groups
    assert builder.delete(f"{P1}/groups/finance/members/{ana}", headers=SAME).status_code == 204
    assert builder.delete(f"{P1}/members/{ana}/attributes/tariff", headers=SAME).status_code == 204
    assert ask(server.client("ana"), detail(10)).json()["result"]["rows"] == []
    assert code(builder.delete(f"{P1}/groups/nothing", headers=SAME)) == (404, "GROUP_NOT_FOUND")
    created = builder.post(f"{P1}/groups", json={"name": "ops"}, headers=SAME)
    assert created.status_code == 201
    assert builder.delete(f"{P1}/groups/ops", headers=SAME).status_code == 204


# --- AC6: reads and the lookup -------------------------------------------------------------------


def test_a_builder_finds_a_user_by_exact_name_only(server: Server) -> None:
    newcomer = server.person("newcomer")
    gone = server.person("gone")
    server.directory.deactivate_user(server.admin, gone)
    builder = server.client("builder")
    found = builder.get(f"{A}/user-lookup", params={"user_name": "NEWCOMER"})
    assert found.json() == {"id": newcomer, "display_name": "Newcomer"}
    misses = []
    for typed in ("newcom", "newcomer*", "gone", "nobody", "", "a b"):
        answer = builder.get(f"{A}/user-lookup", params={"user_name": typed})
        body = answer.json()
        body["error"].pop("request_id", None)
        misses.append((answer.status_code, body))
    assert all(m == misses[0] for m in misses)
    expected = {"code": "USER_NOT_FOUND", "message": "No such user.", "details": []}
    assert misses[0] == (404, {"error": expected})
    server.author("ana")
    author = server.client("ana").get(f"{A}/user-lookup", params={"user_name": "newcomer"})
    assert code(author) == (403, "ACCESS_FORBIDDEN")


def test_the_lookup_budget_answers_429_with_retry_after(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reads, "LOOKUP_BUDGET", 3)
    builder = server.client("builder")
    for typed in ("root", "nobody", "root"):
        builder.get(f"{A}/user-lookup", params={"user_name": typed})
    for typed in ("root", "nobody"):
        limited = builder.get(f"{A}/user-lookup", params={"user_name": typed})
        assert code(limited) == (429, "LOOKUP_LIMITED")
        assert limited.headers["Retry-After"] == "60"


def test_another_projects_builder_reads_nothing_of_it(
    server: Server, people: dict[str, str]
) -> None:
    other = server.client("other")
    for path in (f"{P1}/members", f"{P1}/groups", f"{P1}/members/{people['ana']}/attributes"):
        assert code(other.get(path)) == (403, "ACCESS_FORBIDDEN")
    builder = server.client("builder")
    for user in (people["other"], "u-nobody"):  # not a member, no such user: the same
        assert code(builder.get(f"{P1}/members/{user}/attributes")) == (404, "USER_NOT_FOUND")
    for name in ("ana", "vera"):
        for path in (f"{A}/users", f"{P1}/members", f"{P1}/groups"):
            assert code(server.client(name).get(path)) == (403, "ACCESS_FORBIDDEN")


# --- AC7: request rules and served projects ------------------------------------------------------


def test_a_project_the_instance_does_not_serve_is_not_found_before_any_directory_read(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = server.client("builder")
    asked: list[str] = []
    allowed = reads.DirectoryReads.allowed

    def spied(self: Any, *args: Any) -> bool:  # noqa: ANN401
        asked.append(str(args))
        return allowed(self, *args)

    monkeypatch.setattr(reads.DirectoryReads, "allowed", spied)
    for method, path in (("GET", f"{A}/projects/elsewhere/members"),
                         ("PUT", f"{A}/projects/elsewhere/members/x"),
                         ("GET", f"{A}/projects/elsewhere/groups")):  # fmt: skip
        answer = builder.request(method, path, json={"role": "viewer"}, headers=SAME)
        assert code(answer) == (404, "PROJECT_NOT_SERVED")
    assert asked == []


def test_the_request_rules_hold_on_the_admin_routes(server: Server) -> None:
    root = server.client("root")
    foreign = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert code(root.post(f"{A}/users", json={"user_name": "x", "display_name": "x"},
                          headers=foreign)) == (403, "ORIGIN_REFUSED")  # fmt: skip
    plain = root.post(f"{A}/users", content=b"{}", headers={**SAME, "Content-Type": "text/plain"})
    assert code(plain) == (403, "ORIGIN_REFUSED")
    anonymous = TestClient(server.app, base_url=ORIGIN).get(f"{A}/users")
    assert code(anonymous) == (401, "SESSION_EXPIRED")
    extra = root.post(f"{A}/users", json={"user_name": "x", "display_name": "x", "admin": True},
                      headers=SAME)  # fmt: skip
    assert code(extra) == (422, "VALIDATION_FAILED")
    not_empty = root.post(f"{A}/users/{server.builder}/deactivate", json={"x": 1}, headers=SAME)
    assert code(not_empty) == (422, "VALIDATION_FAILED")


def test_an_application_with_no_served_project_refuses_to_build(server: Server) -> None:
    from orvex.access.directory.reads import DirectoryReads
    from orvex.server.admin_http import Administration

    with pytest.raises(ValueError, match="at least one project"):
        Administration(
            server.directory,
            DirectoryReads(server.store, server.audit),
            server.sign_in,
            server.attributes,
            None,
            frozenset(),
            server.audit,
        )
