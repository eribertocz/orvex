"""The values that cross the access interface contracts (#486 G5): frozen dataclasses of plain
values, never a store, a connection, a key or a callable.

An `Instance` is the handle of one opened access instance (its directory, sessions and audit
log), valid in the implementation that opened it until it is closed. A security context
crosses as its `security-context` v1 document, as canonical JSON text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

Role = Literal["builder", "author", "viewer"]
UserState = Literal["active", "deactivated"]
Audience = Literal["builder", "other"]
Outcome = Literal["allowed", "refused"]
AttributeKind = Literal["text", "integer", "boolean", "list"]
# What a caller may write as an attribute value; the domain checks it, and never coerces it.
AttributeInput = str | bool | int | float | list[str] | tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class SqliteLocation:
    """Local mode's store: one SQLite file."""

    path: str


@dataclass(frozen=True, slots=True)
class PostgresLocation:
    """Server mode's store: a restricted libpq connection string, its password, and the schema
    (the access module's own by default)."""

    connection: str
    password: str
    schema: str | None = None


@dataclass(frozen=True, slots=True)
class OidcSetup:
    """The OIDC configuration (#439): the `oidc.toml` document as text, and the environment
    variables its client secrets are read from."""

    document: str
    environment: dict[str, str]


@dataclass(frozen=True, slots=True)
class Configuration:
    """Where an instance keeps its state: its store, the audit signing key, the anchor file,
    the recovery file, and its OIDC providers (none by default)."""

    store: SqliteLocation | PostgresLocation
    audit_key: str
    audit_anchors: str
    recovery_file: str
    oidc: OidcSetup | None = None


@dataclass(frozen=True, slots=True)
class Instance:
    """The handle of one opened instance."""

    id: str


@dataclass(frozen=True, slots=True)
class User:
    """A user of the instance. `authz_version` grows with every change to what the user may
    do; a session ends when it changes."""

    id: str
    user_name: str
    display_name: str
    state: UserState
    administrator: bool
    authz_version: int
    created_at: str
    changed_at: str


@dataclass(frozen=True, slots=True)
class NewUser:
    """A user just created, and their temporary password: shown once, never stored."""

    user: User
    temporary_password: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A new session: its id, for the cookie only, and whom it is for."""

    session_id: str
    user: User
    password_change_only: bool


@dataclass(frozen=True, slots=True)
class Session:
    """A live session's user as the directory knows them now."""

    user: User
    password_change_only: bool


@dataclass(frozen=True, slots=True)
class SignInSettings:
    """The instance's sign-in settings: session lifetimes, the public origin and the trusted
    proxies (networks in CIDR form)."""

    absolute: timedelta
    idle: timedelta
    public_origin: str | None
    trusted_proxies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleLimit:
    """One role's query limits (#445)."""

    max_concurrent: int
    max_run_ms: int


@dataclass(frozen=True, slots=True)
class UserPage:
    """One page of the instance's users, and the cursor of the next page."""

    users: tuple[User, ...]
    after: str | None


@dataclass(frozen=True, slots=True)
class FoundUser:
    """A user found by name: only what a Builder may learn."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class Member:
    """A project member and their role."""

    user: User
    role: Role


@dataclass(frozen=True, slots=True)
class Attribute:
    """A member attribute as `security-context` v1 types it: `values` for a list, `value`
    otherwise (an integer as its decimal text)."""

    type: AttributeKind
    value: str | bool | None = None
    values: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class IdentityLink:
    """A provider identity linked to a user (#439)."""

    id: str
    provider: str
    subject: str
    linked_at: str


@dataclass(frozen=True, slots=True)
class OidcStarted:
    """Where the browser goes, and the reference of the binding its cookie carries."""

    location: str
    reference: str


@dataclass(frozen=True, slots=True)
class ContextMember:
    """One user's standing in one project, for the fixture contexts tests use."""

    user: str
    project: str
    role: str
    groups: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str | bool | int | tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextFixture:
    """In-memory members, the policy model's fingerprint and the mapping snapshots."""

    policy_fingerprint: str
    mapping_snapshots: dict[str, str]
    members: tuple[ContextMember, ...]


@dataclass(frozen=True, slots=True)
class FingerprintInputs:
    """Everything the permission fingerprint covers (`security-context` v1)."""

    user: str
    project: str
    role: str
    groups: tuple[str, ...]
    attributes_document: str
    policy_fingerprint: str
    mapping_snapshots: dict[str, str]


@dataclass(frozen=True, slots=True)
class LocalRequest:
    """A local-mode request the HTTP layer authenticated with the serve session's token."""

    project: str
    authenticated: bool = False


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """An authenticated server-mode request (a live session) for `project`."""

    user: str
    project: str


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    """What a provider fetcher needs to know of a provider: its hosts and the internal networks
    an authorized internal provider may reach (CIDR form)."""

    name: str
    issuer_host: str
    endpoint_hosts: tuple[str, ...]
    internal: bool
    internal_ranges: tuple[str, ...]
