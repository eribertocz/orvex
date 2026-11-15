"""A server-mode instance with the administration routes (#446), on SQLite or PostgreSQL, for
the tests that need both stores or an OIDC provider: its people sign in with `PASSWORD`, and it
reaches access only through its contract."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from orvex.contracts.access.client import AccessClient
from orvex.contracts.access.kit_world import KitClock
from orvex.contracts.access.values import PostgresLocation, SqliteLocation
from orvex.server.admin_http import Administration
from orvex.server.identity import load_identity
from orvex.server.server_app import create_server_app
from orvex.server.versions import Versions
from orvex.testing.oidc_provider import FakeFetcher, FakeProvider, oidc_setup

from .access_instance import ORIGIN, PASSWORD, AccessInstance, close_opened, postgres_location

SAME = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
SLUG = load_identity().slug
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


class AdminInstance:
    def __init__(self, store: SqliteLocation | PostgresLocation, tmp_path: Path) -> None:
        self.clock = KitClock()
        self.provider = FakeProvider(now=lambda: self.clock().timestamp())
        self.instance = AccessInstance(
            tmp_path,
            store,
            clock=self.clock,
            fetcher=FakeFetcher(self.provider),
            oidc=oidc_setup(),
        )
        self.admin = self.instance.admin_id
        administration = Administration(self.instance.access, frozenset({"project-1"}))
        self.app = create_server_app(
            VERSIONS, self.instance.access, SLUG, administration=administration
        )

    def actor(self, user: str) -> AccessClient:
        return self.instance.actor(user)

    def person(self, name: str) -> str:
        return self.instance.person(name)

    def client(self, name: str) -> TestClient:
        http = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        signed = http.post(
            "/api/v1/auth/login", json={"user_name": name, "password": PASSWORD}, headers=SAME
        )
        assert signed.status_code == 200, signed.text
        return http


@contextmanager
def admin_instance(kind: str, tmp_path: Path) -> Iterator[AdminInstance]:
    if kind == "sqlite":
        yield AdminInstance(SqliteLocation(str(tmp_path / "access.sqlite")), tmp_path)
        return
    with postgres_location() as location:
        yield AdminInstance(location, tmp_path)
        close_opened()  # before its role is dropped
