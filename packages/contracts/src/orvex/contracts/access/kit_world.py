"""What the access and audit-log kits set up through the contracts alone (#486 G1): an instance
in a temporary directory, its first administrator and the first Builder of `PROJECT` signed
in, members, and the kit's OIDC provider.

**The kit's composition.** An implementation runs the kits composed with `KitProviderFetcher`
as its provider fetcher (`orvex.contracts.access.fetcher`) and `KIT_CLOCK` as its clock. The
fetcher answers the discovery document of `KIT_ISSUER` and refuses everything else, so
`oidc_start` succeeds and a callback fails at its first check, as it must, without any network.
The clock only moves forward, when a case advances it (session lifetimes, retention).

A world belongs to one case's client and is made on first use. The worlds of earlier cases are
closed and their directories removed as later ones are made.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orvex.contracts.access.oidc_failure import failed
from orvex.contracts.access.values import (
    Configuration,
    Instance,
    OidcSetup,
    ProviderTarget,
    SqliteLocation,
)
from orvex.contracts.kit_client import KitClient

KIT_ISSUER = "https://idp.kit.invalid/realms/kit"
KIT_PROVIDER = "kit"
PROJECT = "p1"
PASSWORD = "kit-password-long-enough"
ORIGIN = "https://bi.kit.invalid"
_SECRET_VARIABLE = "KIT_OIDC_SECRET"
_KEPT = 2
BUILDER_OPERATIONS = frozenset(
    {
        "members",
        "groups",
        "attributes",
        "find_user",
        "set_membership",
        "remove_membership",
        "create_group",
        "delete_group",
        "add_group_member",
        "remove_group_member",
        "set_attribute",
        "remove_attribute",
    }
)


class KitClock:
    """The time the kit's implementation reads: real time, plus what the cases advanced."""

    def __init__(self) -> None:
        self._ahead = timedelta(0)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return datetime.now(UTC) + self._ahead

    def advance(self, by: timedelta) -> None:
        with self._lock:
            self._ahead += by


KIT_CLOCK = KitClock()


class KitProviderFetcher:
    """The kit's provider: its discovery document, and nothing else."""

    def get(self, provider: ProviderTarget, url: str) -> bytes:
        if url != f"{KIT_ISSUER}/.well-known/openid-configuration":
            raise failed("network")
        base = "https://idp.kit.invalid"
        return json.dumps(
            {
                "issuer": KIT_ISSUER,
                "authorization_endpoint": f"{base}/auth",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/certs",
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
            }
        ).encode()

    def post_form(
        self, provider: ProviderTarget, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes:
        raise failed("token_exchange")


def kit_configuration(directory: Path) -> Configuration:
    document = (
        "[providers.kit]\n"
        f'issuer = "{KIT_ISSUER}"\n'
        'client_id = "kit-client"\n'
        f'client_secret_variable = "{_SECRET_VARIABLE}"\n'
        'algorithms = ["RS256"]\n'
    )
    return Configuration(
        store=SqliteLocation(str(directory / "access.sqlite")),
        audit_key=str(directory / "audit-signing.key"),
        audit_anchors=str(directory / "audit-anchors.log"),
        recovery_file=str(directory / "access-recovery.key"),
        oidc=OidcSetup(document, {_SECRET_VARIABLE: "kit-client-secret"}),
    )


def call(client: KitClient, actor: str, operation: str, *arguments: object) -> Any:  # noqa: ANN401
    """`operation` sent with a new envelope whose actor is `actor`."""
    return client.send(operation, client.envelope(operation, actor=actor), *arguments)


def code_of(client: KitClient, actor: str, operation: str, *arguments: object) -> str | None:
    """The refusal code of one call, or None when it was answered."""
    try:
        call(client, actor, operation, *arguments)
    except Exception as error:  # noqa: BLE001 - the case compares codes
        return str(getattr(error, "code", type(error).__name__))
    return None


@dataclass
class World:
    """One case's instance."""

    client: KitClient
    directory: Path
    configuration: Configuration
    instance: Instance | None = None
    admin: str = ""
    session: str = ""
    builder: str = ""
    builder_session: str = ""
    answers: list[object] = field(default_factory=list)
    prepared: bool = False

    @property
    def opened(self) -> Instance:
        if self.instance is None:
            self.instance = call(self.client, "system", "open", self.configuration)
        return self.instance

    def actor(self, operation: str) -> str:
        """Who performs `operation` in the kit: a project's Builder manages its members, groups
        and attributes; the administrator everything else."""
        return self.builder if operation in BUILDER_OPERATIONS else self.admin

    def close(self) -> None:
        """Close every instance the case opened (one an implementation answered twice for one
        key included), then remove the directory."""
        opened = {answer for answer in self.answers if isinstance(answer, Instance)}
        if self.instance is not None:
            opened.add(self.instance)
        for instance in opened:
            # An instance the case closed itself stays closed.
            with contextlib.suppress(Exception):
                call(self.client, "system", "close", instance)
        shutil.rmtree(self.directory, ignore_errors=True)


_WORLDS: list[World] = []


def world(client: KitClient) -> World:
    """The world of `client`'s case, made on first use."""
    for found in _WORLDS:
        if found.client is client:
            return found
    while len(_WORLDS) >= _KEPT:
        _WORLDS.pop(0).close()
    directory = Path(tempfile.mkdtemp(prefix="access-kit-"))
    made = World(client, directory, kit_configuration(directory))
    _WORLDS.append(made)
    return made


def signed_in(client: KitClient) -> World:
    """The world, with its first administrator created and signed in with a chosen password."""
    found = world(client)
    if found.admin:
        return found
    instance = found.opened
    made = call(client, "system", "create_administrator", instance, "admin", "Admin", False, None)
    first = call(
        client, "anonymous", "login", instance, "admin", made.temporary_password, None, None
    )
    found.admin = made.user.id
    found.session = call(
        client,
        found.admin,
        "change_password",
        instance,
        first.session_id,
        made.temporary_password,
        PASSWORD,
        None,
    )
    call(client, found.admin, "change_sign_in_settings", instance, None, None, ORIGIN, None)
    found.builder, found.builder_session = _signed_up(client, found, "builder", "builder")
    return found


def _signed_up(client: KitClient, found: World, user_name: str, role: str) -> tuple[str, str]:
    instance = found.opened
    made = call(client, found.admin, "create_user", instance, user_name, user_name.title())
    actor = found.admin if not found.builder else found.builder
    call(client, actor, "set_membership", instance, PROJECT, made.user.id, role)
    first = call(
        client, "anonymous", "login", instance, user_name, made.temporary_password, None, None
    )
    session = call(
        client,
        made.user.id,
        "change_password",
        instance,
        first.session_id,
        made.temporary_password,
        PASSWORD,
        None,
    )
    return made.user.id, session


def member(client: KitClient, user_name: str, role: str = "viewer") -> tuple[str, str]:
    """A user with `role` in `PROJECT`, signed in with `PASSWORD`: their id and session."""
    return _signed_up(client, signed_in(client), user_name, role)
