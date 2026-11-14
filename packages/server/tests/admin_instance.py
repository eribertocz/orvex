"""A server-mode instance with the administration routes (#446), on SQLite or PostgreSQL, for
the tests that need both stores or an OIDC provider: its people sign in with `PASSWORD`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from orvex.access.audit import open_audit_log
from orvex.access.authentication.service import SignIn
from orvex.access.authentication.settings import Settings
from orvex.access.directory import Bootstrap, Directory
from orvex.access.directory.attributes import Attributes
from orvex.access.directory.reads import DirectoryReads
from orvex.access.oidc.configuration import parsed
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn
from orvex.access.storage import PostgresStore, SqliteStore, read_settings
from orvex.server.admin_http import Administration
from orvex.server.identity import load_identity
from orvex.server.server_app import create_server_app
from orvex.server.versions import Versions

from tests.support.access_directory import access_role, postgres_connection
from tests.support.oidc_provider import FakeFetcher, FakeProvider
from tests.support.oidc_world import ENVIRONMENT, document
from tests.support.sign_in import PASSWORD, Clock

ORIGIN = "https://bi.example.com"
SAME = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
SLUG = load_identity().slug
VERSIONS = Versions("0.1.0", (("server", "0.1.0"),), (("http-api", "v1"),))


class AdminInstance:
    def __init__(self, store: SqliteStore | PostgresStore, tmp_path: Path) -> None:
        self.clock = Clock()
        self.store = store
        audit = open_audit_log(store, tmp_path / "audit.key", tmp_path / "anchors.log")
        self.audit = audit
        self.sign_in = SignIn(store, audit, self.clock)
        first = Bootstrap(store, tmp_path / "recovery.key", audit).create_administrator(
            "root", "Root"
        )
        self.admin = first.id
        self._changed("root", first.temporary_password)
        self.directory = Directory(store, audit)
        settings = Settings(store, audit)
        settings.change(self.admin, public_origin=ORIGIN)
        self.provider = FakeProvider(now=lambda: self.clock().timestamp())
        configuration = parsed(document(), ENVIRONMENT)
        self.oidc = OidcSignIn(store, audit, configuration, FakeFetcher(self.provider), self.clock)
        self.oidc.start_up()
        self.identities = Identities(store, audit, configuration)
        administration = Administration(
            self.directory,
            DirectoryReads(store, audit),
            self.sign_in,
            Attributes(store, audit),
            self.identities,
            frozenset({"project-1"}),
            audit,
        )
        self.app = create_server_app(
            VERSIONS, self.sign_in, settings, SLUG, self.oidc, administration=administration
        )

    def _changed(self, name: str, temporary: str) -> None:
        signed = self.sign_in.login(name, temporary, "192.0.2.9")
        self.sign_in.change_password(signed.session_id, temporary, PASSWORD, "192.0.2.9")

    def person(self, name: str) -> str:
        made = self.directory.create_user(self.admin, name, name.title())
        self._changed(name, made.temporary_password)
        return made.id

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
        store: SqliteStore | PostgresStore = SqliteStore(tmp_path / "access.sqlite")
        try:
            yield AdminInstance(store, tmp_path)
        finally:
            store.close()
        return
    with access_role(*postgres_connection()) as (connection, password):
        store = PostgresStore(
            read_settings(connection, password, schema=f"t_{uuid.uuid4().hex[:16]}")
        )
        try:
            yield AdminInstance(store, tmp_path)
        finally:
            store.close()
