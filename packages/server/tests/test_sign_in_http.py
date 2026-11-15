"""#438 AC3, AC4, AC5, AC6, R2, R3 over HTTP: server mode's cookie, its refusals of cross-site
requests and of state changes through safe methods, the temporary-password gate, the delay's
answer, and which address a request is counted under; on SQLite and on PostgreSQL."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orvex.contracts.access.kit_world import KitClock
from orvex.contracts.access.values import PostgresLocation, SqliteLocation
from orvex.server.client_address import client_address
from orvex.server.identity import load_identity
from orvex.server.server_app import checked_route_table, create_server_app
from orvex.server.sign_in_http import cookie_name
from orvex.server.versions import Versions

from tests.support.postgres_roles import postgres_location

from .access_instance import PASSWORD, AccessInstance, close_opened

SLUG = load_identity().slug
COOKIE = cookie_name(SLUG)
ORIGIN = "https://bi.example.com"
PEER = "203.0.113.5"
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))
SAME = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}


class Instance:
    """A server-mode application over an access instance on `store`, with a clock the tests
    move."""

    def __init__(
        self, store: SqliteLocation | PostgresLocation, tmp_path: Path, origin: str | None = ORIGIN
    ) -> None:
        self.clock = KitClock()
        self.access = AccessInstance(tmp_path, store, clock=self.clock, origin=origin)
        self.admin = self.access.admin
        self.app = create_server_app(VERSIONS, self.access.access, SLUG)

    def client(self, peer: str = PEER) -> TestClient:
        return TestClient(self.app, base_url=ORIGIN, client=(peer, 50000))

    def user(self, name: str) -> str:
        """A user with `PASSWORD`, set through their temporary one."""
        return self.access.person(name)


@pytest.fixture(params=["sqlite", "postgres"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[SqliteLocation | PostgresLocation]:
    if request.param == "sqlite":
        yield SqliteLocation(str(tmp_path / "access.sqlite"))
        return
    with postgres_location() as location:
        yield location
        close_opened()  # before its role is dropped


@pytest.fixture
def instance(store: SqliteLocation | PostgresLocation, tmp_path: Path) -> Instance:
    return Instance(store, tmp_path)


def login(client: TestClient, name: str, password: str = PASSWORD) -> int:
    answer = client.post(
        "/api/v1/auth/login", json={"user_name": name, "password": password}, headers=SAME
    )
    return answer.status_code


def test_the_cookie_has_every_attribute(instance: Instance) -> None:
    instance.user("ana")
    client = instance.client()
    answer = client.post(
        "/api/v1/auth/login", json={"user_name": "ana", "password": PASSWORD}, headers=SAME
    )
    assert answer.status_code == 200
    assert answer.json()["password_change_required"] is False
    cookie = answer.headers["set-cookie"]
    name, rest = cookie.split("=", 1)
    assert name == COOKIE
    attributes = {part.strip().split("=")[0].lower() for part in rest.split(";")[1:]}
    assert attributes == {"httponly", "max-age", "path", "samesite", "secure"}
    assert "Path=/" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=43200" in cookie
    session_id = rest.split(";")[0]
    assert session_id not in answer.text
    assert client.get("/api/v1/auth/session").json()["user"]["user_name"] == "ana"


def test_logout_ends_the_session_and_clears_the_cookie(instance: Instance) -> None:
    instance.user("ana")
    client = instance.client()
    assert login(client, "ana") == 200
    session_id = client.cookies[COOKIE]
    answer = client.post("/api/v1/auth/logout", json={}, headers=SAME)
    assert answer.status_code == 204
    assert "Max-Age=0" in answer.headers["set-cookie"]
    client.cookies.set(COOKIE, session_id)
    gone = client.get("/api/v1/auth/session")
    assert (gone.status_code, gone.json()["error"]["code"]) == (401, "SESSION_EXPIRED")


@pytest.mark.parametrize(
    ("headers", "header"),
    [
        ({}, "origin"),
        ({"Origin": "https://evil.example"}, "origin"),
        ({"Origin": "https://bi.example.com:8443"}, "origin"),
        ({"Origin": "http://bi.example.com"}, "origin"),
        ({"Origin": "null"}, "origin"),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, "sec-fetch-site"),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, "sec-fetch-site"),
    ],
    ids=[
        "no origin",
        "other site",
        "other port",
        "other scheme",
        "null",
        "cross-site",
        "same-site",
    ],
)
def test_a_state_change_from_anywhere_else_is_refused(
    instance: Instance, headers: dict[str, str], header: str
) -> None:
    instance.user("ana")
    client = instance.client()
    answer = client.post(
        "/api/v1/auth/login", json={"user_name": "ana", "password": PASSWORD}, headers=headers
    )
    assert answer.status_code == 403
    error = answer.json()["error"]
    assert error["code"] == "ORIGIN_REFUSED"
    assert error["details"][0]["name"] == header
    assert COOKIE not in answer.headers.get("set-cookie", "")


def test_a_form_encoded_body_is_refused(instance: Instance) -> None:
    instance.user("ana")
    answer = instance.client().post(
        "/api/v1/auth/login", data={"user_name": "ana", "password": PASSWORD}, headers=SAME
    )
    assert answer.status_code == 403
    assert answer.json()["error"]["details"] == [
        {"name": "content-type", "value": "application/x-www-form-urlencoded"}
    ]


def test_until_the_public_origin_is_set_every_state_change_is_refused(
    store: SqliteLocation | PostgresLocation, tmp_path: Path
) -> None:
    bare = Instance(store, tmp_path, origin=None)
    answer = bare.client().post(
        "/api/v1/auth/login", json={"user_name": "root", "password": PASSWORD}, headers=SAME
    )
    assert (answer.status_code, answer.json()["error"]["code"]) == (403, "ORIGIN_REFUSED")


def test_a_state_change_through_get_is_refused(instance: Instance) -> None:
    instance.user("ana")
    client = instance.client()
    assert login(client, "ana") == 200
    answer = client.get("/api/v1/auth/logout")
    assert answer.status_code == 405
    assert answer.headers["allow"] == "POST"
    assert client.get("/api/v1/auth/session").status_code == 200  # still signed in


def test_a_route_mixing_safe_and_state_changing_methods_is_refused() -> None:
    app = FastAPI()

    @app.api_route("/api/v1/mixed", methods=["GET", "POST"])
    async def mixed() -> None:
        return None

    with pytest.raises(ValueError, match="mixes"):
        checked_route_table(app)


def test_without_a_live_session_an_api_route_is_session_expired(instance: Instance) -> None:
    client = instance.client()
    answers = [client.get("/api/v1/auth/session")]
    client.cookies.set(COOKIE, "made-up")
    answers.append(client.get("/api/v1/auth/session"))
    answers.append(client.get("/api/v1/anything"))
    bodies = [
        (a.status_code, a.json()["error"]["code"], a.json()["error"]["details"]) for a in answers
    ]
    assert bodies == [(401, "SESSION_EXPIRED", [])] * 3
    assert client.get("/api/v1/health").status_code == 200


def test_a_temporary_password_session_may_only_change_it(instance: Instance) -> None:
    created = instance.admin.create_user("ana", "Ana")

    @instance.app.get("/api/v1/projects")
    async def projects() -> dict[str, str]:
        return {"ok": "yes"}

    client = instance.client()
    signed = client.post(
        "/api/v1/auth/login",
        json={"user_name": "ana", "password": created.temporary_password},
        headers=SAME,
    )
    assert signed.json()["password_change_required"] is True
    refused = client.get("/api/v1/projects")
    assert (refused.status_code, refused.json()["error"]["code"]) == (
        403,
        "PASSWORD_CHANGE_REQUIRED",
    )
    old = client.cookies[COOKIE]
    changed = client.post(
        "/api/v1/auth/password",
        json={"current_password": created.temporary_password, "new_password": PASSWORD},
        headers=SAME,
    )
    assert changed.status_code == 204
    assert client.cookies[COOKIE] != old
    assert client.get("/api/v1/projects").json() == {"ok": "yes"}


def test_a_rejected_new_password_says_which_rule(instance: Instance) -> None:
    instance.user("ana")
    client = instance.client()
    assert login(client, "ana") == 200
    answer = client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "ana"},
        headers=SAME,
    )
    assert answer.status_code == 400
    assert answer.json()["error"]["code"] == "PASSWORD_REJECTED"
    assert answer.json()["error"]["details"] == [{"name": "rule", "value": "length"}]


def test_a_delayed_sign_in_is_429_with_retry_after_and_one_body_for_all(
    instance: Instance,
) -> None:
    instance.user("ana")
    assert login(instance.client("192.0.2.1"), "ana", "wrong") == 401
    assert login(instance.client("192.0.2.2"), "nobody", "wrong") == 401
    answers = [
        instance.client(peer).post(
            "/api/v1/auth/login", json={"user_name": name, "password": PASSWORD}, headers=SAME
        )
        for peer, name in (("192.0.2.3", "ana"), ("192.0.2.4", "nobody"))
    ]
    for answer in answers:
        assert answer.status_code == 429
        assert int(answer.headers["retry-after"]) >= 1
    bodies = [{**a.json()["error"], "request_id": ""} for a in answers]
    assert bodies[0] == bodies[1]
    assert bodies[0]["code"] == "LOGIN_DELAYED"
    assert bodies[0]["details"] == []


def test_a_forwarded_for_from_an_untrusted_peer_is_ignored(instance: Instance) -> None:
    instance.user("ana")
    client = instance.client()
    for number, name in enumerate(("u1", "u2")):
        client.post(
            "/api/v1/auth/login",
            json={"user_name": name, "password": "wrong"},
            headers={**SAME, "X-Forwarded-For": f"198.51.100.{number}"},
        )
    delayed = client.post(
        "/api/v1/auth/login",
        json={"user_name": "ana", "password": PASSWORD},
        headers={**SAME, "X-Forwarded-For": "198.51.100.200"},
    )
    assert delayed.status_code == 429  # counted under the peer, whatever the header says


def test_a_trusted_proxys_forwarded_for_names_the_client(instance: Instance) -> None:
    instance.user("ana")
    instance.admin.change_sign_in_settings(trusted_proxies=(f"{PEER}/32",))
    client = instance.client()
    client.post(
        "/api/v1/auth/login",
        json={"user_name": "u1", "password": "wrong"},
        headers={**SAME, "X-Forwarded-For": "198.51.100.1"},
    )
    answer = client.post(
        "/api/v1/auth/login",
        json={"user_name": "ana", "password": PASSWORD},
        headers={**SAME, "X-Forwarded-For": "6.6.6.6, 198.51.100.2"},
    )
    assert answer.status_code == 200  # another client behind the same proxy


def test_no_password_or_session_id_reaches_the_log(
    instance: Instance, caplog: pytest.LogCaptureFixture
) -> None:
    instance.user("ana")
    client = instance.client()
    with caplog.at_level(logging.DEBUG):
        login(client, "ana", "a wrong password")
        instance.clock.advance(timedelta(seconds=2))
        login(client, "ana")
        client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "another passphrase"},
            headers=SAME,
        )
    logged = caplog.text + "".join(str(vars(record)) for record in caplog.records)
    for secret in (PASSWORD, "a wrong password", "another passphrase", client.cookies[COOKIE]):
        assert secret not in logged


@pytest.mark.parametrize(
    ("peer", "forwarded", "proxies", "expected"),
    [
        ("203.0.113.5", "198.51.100.1", [], "203.0.113.5"),
        ("203.0.113.5", "198.51.100.1", ["203.0.113.0/24"], "198.51.100.1"),
        ("203.0.113.5", "6.6.6.6, 198.51.100.1", ["203.0.113.0/24"], "198.51.100.1"),
        ("203.0.113.5", "198.51.100.1, 203.0.113.9", ["203.0.113.0/24"], "198.51.100.1"),
        ("203.0.113.5", "garbage, 198.51.100.1", ["203.0.113.0/24"], "198.51.100.1"),
        ("203.0.113.5", "198.51.100.1, garbage", ["203.0.113.0/24"], "203.0.113.5"),
        ("203.0.113.5", "203.0.113.7", ["203.0.113.0/24"], "203.0.113.7"),
        ("203.0.113.5", None, ["203.0.113.0/24"], "203.0.113.5"),
        ("2001:db8::5", "198.51.100.1", ["2001:db8::/32"], "198.51.100.1"),
        ("testclient", "198.51.100.1", ["203.0.113.0/24"], "testclient"),
    ],
)
def test_the_client_address(
    peer: str, forwarded: str | None, proxies: list[str], expected: str
) -> None:
    assert client_address(peer, [] if forwarded is None else [forwarded], proxies) == expected


def test_every_forwarded_for_line_counts_in_order() -> None:
    """A proxy that adds its own line: the client's line comes first and is not believed."""
    networks = ["203.0.113.0/24"]
    assert client_address("203.0.113.5", ["9.9.9.9", "198.51.100.20"], networks) == (
        "198.51.100.20"
    )


def test_a_trusted_proxys_added_line_names_the_client_over_http(instance: Instance) -> None:
    instance.user("ana")
    instance.admin.change_sign_in_settings(trusted_proxies=(f"{PEER}/32",))
    client = instance.client()
    for spoofed in ("9.9.9.1", "9.9.9.2"):
        client.post(
            "/api/v1/auth/login",
            json={"user_name": "u1", "password": "wrong"},
            headers=[
                *SAME.items(),
                ("Content-Type", "application/json"),
                ("X-Forwarded-For", spoofed),
                ("X-Forwarded-For", "198.51.100.7"),
            ],
        )
    delayed = client.post(
        "/api/v1/auth/login",
        content=b'{"user_name": "ana", "password": "%s"}' % PASSWORD.encode(),
        headers=[
            *SAME.items(),
            ("Content-Type", "application/json"),
            ("X-Forwarded-For", "9.9.9.3"),
            ("X-Forwarded-For", "198.51.100.7"),
        ],
    )
    assert delayed.status_code == 429  # one client, whatever it wrote first
