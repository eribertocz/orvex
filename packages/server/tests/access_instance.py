"""An access instance for the server tests, reached only through the access contract
(`orvex.contracts.access.client`): opened in a folder on SQLite or on a PostgreSQL schema of its
own, its administrator `root` signed up with `PASSWORD`, and its people. Every instance a test
opens is closed when the test ends (`close_opened`, the suite's autouse fixture)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from orvex.contracts.access.client import AccessClient, system
from orvex.contracts.access.fetcher import ProviderFetcher
from orvex.contracts.access.values import (
    Configuration,
    OidcSetup,
    PostgresLocation,
    SqliteLocation,
)
from orvex.server.assembly import access_bindings
from orvex.server.identity import load_identity
from orvex.testing.postgres_roles import access_role, postgres_connection

IDENTITY = load_identity()
PASSWORD = "a long enough passphrase"
ADDRESS = "192.0.2.9"
ORIGIN = "https://bi.example.com"


OPENED: list[AccessInstance] = []


def close_opened() -> None:
    """Close every instance opened since the last call."""
    while OPENED:
        OPENED.pop().close()


def local_contexts() -> AccessClient:
    """The access contract, for local mode's contexts, which need no instance."""
    return AccessClient(*access_bindings(IDENTITY), system("server-tests"))


class AccessInstance:
    """One opened instance; `admin` acts as its first administrator. Its public origin is
    `origin` (none: every state change is refused until one is set)."""

    def __init__(
        self,
        folder: Path,
        store: SqliteLocation | PostgresLocation | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        fetcher: ProviderFetcher | None = None,
        oidc: OidcSetup | None = None,
        origin: str | None = ORIGIN,
    ) -> None:
        self.configuration = Configuration(
            store=store or SqliteLocation(str(folder / "access.sqlite")),
            audit_key=str(folder / "audit.key"),
            audit_anchors=str(folder / "anchors.log"),
            recovery_file=str(folder / "recovery.key"),
            oidc=oidc,
        )
        bindings = access_bindings(IDENTITY, clock=clock, fetcher=fetcher)
        self.access = AccessClient(*bindings, system("server-tests")).open(self.configuration)
        OPENED.append(self)
        first = self.access.create_administrator("root", "Root")
        self.admin_id = first.user.id
        self.admin = self.access.as_actor(self.admin_id)
        self.signed_up("root", first.temporary_password)
        if origin is not None:
            self.admin.change_sign_in_settings(public_origin=origin)

    def actor(self, user: str) -> AccessClient:
        """The instance, for `user`."""
        return self.access.as_actor(user)

    def signed_up(self, user_name: str, temporary: str) -> None:
        """The person's temporary password replaced by `PASSWORD`."""
        signed = self.access.login(user_name, temporary, ADDRESS)
        self.actor(signed.user.id).change_password(signed.session_id, temporary, PASSWORD, ADDRESS)

    def person(self, name: str) -> str:
        """A new user, signed up with `PASSWORD`: their id."""
        made = self.admin.create_user(name, name.title())
        self.signed_up(name, made.temporary_password)
        return made.user.id

    def events(self) -> list[dict[str, Any]]:
        """Every audit event of the instance, in order."""
        return [json.loads(event) for event in self.access.events()]

    def close(self) -> None:
        self.access.close()


@contextmanager
def postgres_location() -> Iterator[PostgresLocation]:
    """An access store location on PostgreSQL: a disposable role of its own and a schema of
    its own (`orvex.testing.postgres_roles`)."""
    with access_role(*postgres_connection()) as (connection, password):
        yield PostgresLocation(connection, password, f"t_{uuid.uuid4().hex[:16]}")
