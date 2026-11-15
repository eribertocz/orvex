"""#439 over HTTP: the OIDC routes' cookies and pages, the generic failure page, an unknown
provider, and the silent re-authentication that the session gate asks for; on SQLite and on
PostgreSQL, against the in-process provider."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from orvex.contracts.access.kit_world import KitClock
from orvex.contracts.access.values import PostgresLocation, SqliteLocation
from orvex.server.identity import load_identity
from orvex.server.oidc_http import binding_cookie
from orvex.server.server_app import create_server_app
from orvex.server.sign_in_http import cookie_name
from orvex.server.versions import Versions
from orvex.testing.oidc_provider import FakeFetcher, FakeProvider, oidc_setup

from .access_instance import AccessInstance, close_opened, postgres_location

SLUG = load_identity().slug
SESSION = cookie_name(SLUG)
BINDING = binding_cookie(SLUG)
ORIGIN = "https://bi.example.com"
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


class Instance:
    def __init__(self, store: SqliteLocation | PostgresLocation, tmp_path: Path) -> None:
        self.clock = KitClock()
        self.provider = FakeProvider(now=lambda: self.clock().timestamp())
        self.access = AccessInstance(
            tmp_path,
            store,
            clock=self.clock,
            fetcher=FakeFetcher(self.provider),
            oidc=oidc_setup(),
        )
        self.app = create_server_app(VERSIONS, self.access.access, SLUG)

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=ORIGIN, follow_redirects=False)

    def linked(self, name: str, subject: str) -> str:
        user = self.access.admin.create_user(name, name.title()).user.id
        self.access.admin.link_identity(user, "corp", subject)
        return user

    def sign_in(self, client: TestClient, subject: str, refresh: bool = False) -> int:
        started = client.get(
            "/api/v1/auth/oidc/corp/start", params={"refresh": "true"} if refresh else None
        )
        assert started.status_code == 303
        answer = self.provider.authorize(started.headers["location"], subject)
        return client.get("/api/v1/auth/oidc/corp/callback", params=answer).status_code


@pytest.fixture(params=["sqlite", "postgres"])
def instance(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Instance]:
    if request.param == "sqlite":
        yield Instance(SqliteLocation(str(tmp_path / "access.sqlite")), tmp_path)
        return
    with postgres_location() as location:
        yield Instance(location, tmp_path)
        close_opened()  # before its role is dropped


def test_start_redirects_with_the_binding_cookie(instance: Instance) -> None:
    started = instance.client().get("/api/v1/auth/oidc/corp/start")
    assert started.status_code == 303
    query = parse_qs(urlsplit(started.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    cookie = started.headers["set-cookie"]
    assert cookie.startswith(f"{BINDING}=")
    for attribute in (
        "HttpOnly",
        "Max-Age=600",
        "Path=/api/v1/auth/oidc",
        "SameSite=lax",
        "Secure",
    ):
        assert attribute in cookie
    assert "Domain" not in cookie


def test_a_callback_opens_a_session_and_clears_the_binding(instance: Instance) -> None:
    instance.linked("ana", "sub-ana")
    client = instance.client()
    started = client.get("/api/v1/auth/oidc/corp/start")
    answer = instance.provider.authorize(started.headers["location"], "sub-ana")
    done = client.get("/api/v1/auth/oidc/corp/callback", params=answer)
    assert done.status_code == 200
    assert "url=/" in done.text
    cookies = done.headers.get_list("set-cookie")
    assert any(c.startswith(f"{SESSION}=") and "SameSite=strict" in c for c in cookies)
    assert any(c.startswith(f"{BINDING}=") and "Max-Age=0" in c for c in cookies)
    assert client.get("/api/v1/auth/session").json()["user"]["user_name"] == "ana"


def test_every_failure_is_one_generic_page(instance: Instance) -> None:
    client = instance.client()
    started = client.get("/api/v1/auth/oidc/corp/start")
    answer = instance.provider.authorize(started.headers["location"], "nobody-linked")
    unlinked = client.get("/api/v1/auth/oidc/corp/callback", params=answer)
    forged = client.get("/api/v1/auth/oidc/corp/callback", params={"state": "x", "code": "y"})
    assert unlinked.status_code == forged.status_code == 400
    assert unlinked.text == forged.text
    assert "OIDC_FAILED" in unlinked.text
    for word in ("unlinked", "UNLINKED", "state", "STATE"):
        assert word not in unlinked.text


def test_a_provider_that_is_not_configured(instance: Instance) -> None:
    answer = instance.client().get("/api/v1/auth/oidc/other/start")
    assert (answer.status_code, answer.json()["error"]["code"]) == (404, "OIDC_NOT_CONFIGURED")


def test_the_gate_asks_for_a_silent_reauthentication_when_groups_are_due(
    instance: Instance,
) -> None:
    instance.linked("ana", "sub-ana")
    client = instance.client()
    assert instance.sign_in(client, "sub-ana") == 200
    instance.clock.advance(timedelta(minutes=30))
    assert client.get("/api/v1/auth/session").status_code == 200  # not idle
    instance.clock.advance(timedelta(minutes=30))
    due = client.get("/api/v1/auth/session")
    assert due.status_code == 401
    assert due.json()["error"]["code"] == "OIDC_REFRESH_REQUIRED"
    assert due.json()["error"]["details"] == [{"name": "provider", "value": "corp"}]
    started = client.get("/api/v1/auth/oidc/corp/start", params={"refresh": "true"})
    assert "prompt=none" in started.headers["location"]
    answer = instance.provider.authorize(started.headers["location"], "sub-ana")
    assert client.get("/api/v1/auth/oidc/corp/callback", params=answer).status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 200


def test_a_failed_silent_reauthentication_ends_the_session(instance: Instance) -> None:
    instance.linked("ana", "sub-ana")
    client = instance.client()
    instance.sign_in(client, "sub-ana")
    instance.clock.advance(timedelta(hours=1))
    started = client.get("/api/v1/auth/oidc/corp/start", params={"refresh": "true"})
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    failed = client.get(
        "/api/v1/auth/oidc/corp/callback",
        params={"error": "login_required", "state": state, "iss": instance.provider.issuer},
    )
    assert failed.status_code == 400
    ended = client.get("/api/v1/auth/session")
    assert (ended.status_code, ended.json()["error"]["code"]) == (401, "SESSION_EXPIRED")


def test_a_refresh_that_is_not_due_is_refused(instance: Instance) -> None:
    instance.linked("ana", "sub-ana")
    client = instance.client()
    instance.sign_in(client, "sub-ana")
    assert client.get("/api/v1/auth/oidc/corp/start", params={"refresh": "true"}).status_code == 400
    assert client.get("/api/v1/auth/session").status_code == 200
