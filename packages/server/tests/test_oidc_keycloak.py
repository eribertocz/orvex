"""#439 AC1, AC10: the whole OIDC flow against a real identity provider, Keycloak, which the
`python-oidc` CI job runs from an image pinned by digest with the realm fixture of
`tests/fixtures/keycloak/` (two synthetic users, two provider groups, one confidential client).

The provider is reached at `https://keycloak.test:8443` through `ProviderFetch` with its
certificate verified. Only the address step is replaced: the resolver answers a public address
and the connector reaches the container on loopback, because loopback is, rightly, refused (the
address rules have their own tests in `test_provider_fetch.py`). The browser's side (the login
form, Keycloak's own cookies) is a plain HTTP client.

Outside that job the variables are absent and the tests skip; inside it, `KEYCLOAK_TEST_URL`
is set and a missing piece fails them.
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import ssl
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from orvex.access.audit import open_audit_log
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.directory import Bootstrap, Directory, statements
from orvex.access.oidc.configuration import parsed
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn
from orvex.access.storage import SqliteStore
from orvex.server.identity import load_identity
from orvex.server.provider_fetch import ProviderFetch
from orvex.server.server_app import create_server_app
from orvex.server.versions import Versions

KEYCLOAK = os.environ.get("KEYCLOAK_TEST_URL", "")
CA = os.environ.get("KEYCLOAK_TEST_CA", "")
ADMIN_PASSWORD = os.environ.get("KEYCLOAK_TEST_ADMIN_PASSWORD", "")
pytestmark = pytest.mark.skipif(not KEYCLOAK, reason="needs the python-oidc job's Keycloak")

REALM = "ci-realm"
ORIGIN = "https://bi.example.com"
SLUG = load_identity().slug
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


def _trust() -> ssl.SSLContext:
    """The job's certificate authority, as httpx2 takes it (a path string is deprecated)."""
    return ssl.create_default_context(cafile=CA)


def _fetcher() -> ProviderFetch:
    port = urlsplit(KEYCLOAK).port or 443

    def connect(address: str, _port: int, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    return ProviderFetch(
        context=_trust(),
        resolver=lambda host, port: ["93.184.215.14"],
        connector=connect,
    )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.store = SqliteStore(tmp_path / "access.sqlite")
        audit = open_audit_log(self.store, tmp_path / "audit.key", tmp_path / "anchors.log")
        self.admin = (
            Bootstrap(self.store, tmp_path / "recovery.key", audit)
            .create_administrator("root", "Root")
            .id
        )
        directory = Directory(self.store, audit)
        settings = Settings(self.store, audit)
        settings.change(self.admin, public_origin=ORIGIN)
        configuration = parsed(
            {
                "providers": {
                    "kc": {
                        "issuer": f"{KEYCLOAK}/realms/{REALM}",
                        "client_id": "bi-client",
                        "client_secret_variable": "KC_SECRET",
                        "algorithms": ["RS256"],
                    }
                },
                "mapping": [
                    {"provider": "kc", "claim": "groups", "value": v, "project": "p1", "group": v}
                    for v in ("finance", "ops")
                ],
            },
            {"KC_SECRET": "ci-disposable-client-secret"},
        )
        self.oidc = OidcSignIn(self.store, audit, configuration, _fetcher())
        self.oidc.start_up()
        builder = directory.create_user(self.admin, "builder", "Builder").id
        directory.add_membership(self.admin, "p1", builder, "builder")
        for group in ("finance", "ops"):
            directory.create_group(builder, "p1", group)
        self.alice = directory.create_user(self.admin, "alice", "Alice").id
        directory.add_membership(builder, "p1", self.alice, "viewer")
        Identities(self.store, audit, configuration).link(
            self.admin, self.alice, "kc", _subject("alice")
        )
        app = create_server_app(VERSIONS, SignIn(self.store, audit), settings, SLUG, self.oidc)
        self.app = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        self.browser = httpx2.Client(verify=_trust(), follow_redirects=False, timeout=20)

    def groups(self) -> set[str]:
        with self.store.transaction(read_only=True) as transaction:
            return set(statements.groups_of(transaction, "p1", self.alice))

    def provider_answer(self, location: str, password: str | None) -> dict[str, str]:
        """Keycloak's redirect to the callback, through its login form when `password`."""
        answer = self.browser.get(location)
        if password is not None and answer.status_code == 200:
            form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', answer.text)
            assert form is not None, "no login form"
            answer = self.browser.post(
                html.unescape(form.group(1)),
                data={"username": "alice", "password": password, "credentialId": ""},
            )
        assert answer.status_code in (302, 303), answer.text[:200]
        target = answer.headers["location"]
        assert target.startswith(f"{ORIGIN}/api/v1/auth/oidc/kc/callback")
        return {key: values[0] for key, values in parse_qs(urlsplit(target).query).items()}

    def sign_in(self, refresh: bool = False, password: str | None = None) -> int:
        started = self.app.get(
            "/api/v1/auth/oidc/kc/start", params={"refresh": "true"} if refresh else None
        )
        assert started.status_code == 303
        answer = self.provider_answer(started.headers["location"], password)
        return self.app.get("/api/v1/auth/oidc/kc/callback", params=answer).status_code

    def close(self) -> None:
        self.browser.close()
        self.app.close()
        self.store.close()


def _admin_token() -> str:
    with httpx2.Client(verify=_trust(), timeout=20) as client:
        answer = client.post(
            f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": ADMIN_PASSWORD,
            },
        )
        answer.raise_for_status()
        return str(answer.json()["access_token"])


def _admin(method: str, path: str) -> object:
    with httpx2.Client(verify=_trust(), timeout=20) as client:
        answer = client.request(
            method,
            f"{KEYCLOAK}/admin/realms/{REALM}{path}",
            headers={"Authorization": f"Bearer {_admin_token()}"},
        )
        answer.raise_for_status()
        return json.loads(answer.text) if answer.text else None


def _subject(user: str) -> str:
    found = _admin("GET", f"/users?username={user}&exact=true")
    assert isinstance(found, list)
    assert len(found) == 1
    return str(found[0]["id"])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    made = World(tmp_path)
    yield made
    made.close()


def _make_due(world: World) -> None:
    """The group-refresh period has passed: rather than move a clock past Keycloak's own token
    lifetimes, the session's last refresh is set an hour back."""
    with world.store.transaction() as transaction:
        transaction.run("UPDATE oidc_sessions SET refreshed_at = '2000-01-01T00:00:00.000000Z'")


def test_the_whole_flow_against_keycloak(world: World) -> None:
    assert world.sign_in(password="alice-disposable-password") == 200
    session = world.app.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert session.json()["user"]["user_name"] == "alice"
    assert world.groups() == {"finance"}


def test_a_group_removed_at_keycloak_is_gone_after_the_refresh(world: World) -> None:
    assert world.sign_in(password="alice-disposable-password") == 200
    user = _subject("alice")
    groups = _admin("GET", f"/users/{user}/groups")
    assert isinstance(groups, list)
    finance = next(group["id"] for group in groups if group["name"] == "finance")
    _admin("DELETE", f"/users/{user}/groups/{finance}")
    try:
        _make_due(world)
        assert (
            world.app.get("/api/v1/auth/session").json()["error"]["code"] == "OIDC_REFRESH_REQUIRED"
        )
        assert world.sign_in(refresh=True) == 200  # silent: Keycloak's own session, prompt=none
        assert world.groups() == set()
        assert world.app.get("/api/v1/auth/session").status_code == 200
    finally:
        _admin("PUT", f"/users/{user}/groups/{finance}")


def test_a_failed_silent_reauthentication_ends_the_session(world: World) -> None:
    assert world.sign_in(password="alice-disposable-password") == 200
    world.browser.cookies.clear()  # signed out at Keycloak: prompt=none answers login_required
    _make_due(world)
    assert world.sign_in(refresh=True) == 400
    ended = world.app.get("/api/v1/auth/session")
    assert ended.json()["error"]["code"] == "SESSION_EXPIRED"
