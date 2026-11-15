"""The consumer's side of the `access` and `audit-log` interface contracts: `AccessClient`, every
call sent through its binding with a complete call envelope (#486 §2 item 6).

A consumer is given the two bindings of the chosen implementation (`server/assembly.py`) and the
call context it acts in (`orvex.contracts.context.CallContext`, built at its entry point); it
never knows the module behind them. `open` returns the client of the
instance it opened; every other operation acts on that instance. Each call that changes state
carries a new idempotency key. The caller's `actor` is the effective actor of the operation:
the signed-in user, `UNAUTHENTICATED` before a sign-in, `SYSTEM` for the instance's own work.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from orvex.contracts.access.interface import ACCESS, SYSTEM, UNAUTHENTICATED
from orvex.contracts.access.values import (
    Attribute,
    AttributeInput,
    Audience,
    Configuration,
    ContextFixture,
    FingerprintInputs,
    FoundUser,
    IdentityLink,
    Instance,
    Member,
    NewUser,
    OidcStarted,
    Outcome,
    RoleLimit,
    Session,
    SignedIn,
    SignInSettings,
    User,
    UserPage,
)
from orvex.contracts.audit_log.interface import AUDIT_LOG
from orvex.contracts.binding import Bound
from orvex.contracts.context import CallContext
from orvex.contracts.envelope import CallEnvelope

# The project and revision of a call that acts on the instance itself, not on one project.
INSTANCE_PROJECT = "instance"
INSTANCE_REVISION = "instance"


def system(correlation_id: str) -> CallContext:
    """The instance's own work, under `correlation_id`."""
    return CallContext(correlation_id, SYSTEM, INSTANCE_PROJECT, INSTANCE_REVISION)


def unauthenticated(correlation_id: str) -> CallContext:
    """A call before any sign-in, under `correlation_id`."""
    return CallContext(correlation_id, UNAUTHENTICATED, INSTANCE_PROJECT, INSTANCE_REVISION)


def signed_in(user: str, correlation_id: str) -> CallContext:
    """A signed-in user's call on the instance, under `correlation_id`."""
    return CallContext(correlation_id, user, INSTANCE_PROJECT, INSTANCE_REVISION)


class AccessClient:
    """The access instance `instance`, reached through `access` and `audit` for `caller`."""

    __slots__ = ("_access", "_audit", "_caller", "_instance")

    def __init__(
        self, access: Bound, audit: Bound, caller: CallContext, instance: Instance | None = None
    ) -> None:
        if access.contract is not ACCESS or audit.contract is not AUDIT_LOG:
            raise ValueError("an access client needs the access and audit-log contracts")
        self._access = access
        self._audit = audit
        self._caller = caller
        self._instance = instance

    @property
    def caller(self) -> CallContext:
        return self._caller

    @property
    def instance(self) -> Instance:
        if self._instance is None:
            raise ValueError("the client has no instance: open one first")
        return self._instance

    def acting_for(self, caller: CallContext) -> AccessClient:
        """The same instance, for another caller."""
        return AccessClient(self._access, self._audit, caller, self._instance)

    def as_actor(self, actor: str, project_id: str | None = None) -> AccessClient:
        """The same instance and correlation, for `actor` (and `project_id`, when given)."""
        project = self._caller.project_id if project_id is None else project_id
        return self.acting_for(replace(self._caller, actor=actor, project_id=project))

    def _envelope(self, bound: Bound, operation: str) -> CallEnvelope:
        changes = bound.contract.operation(operation).changes_state
        key = uuid.uuid4().hex if changes else None
        return self._caller.envelope(bound.contract, operation, key)

    def call(self, operation: str, /, *arguments: object) -> Any:  # noqa: ANN401 - its result
        """An `access` operation on this instance."""
        envelope = self._envelope(self._access, operation)
        return self._access.call(operation, envelope, self.instance, *arguments)

    def audit_call(self, operation: str, /, *arguments: object) -> Any:  # noqa: ANN401
        """An `audit-log` operation on this instance."""
        envelope = self._envelope(self._audit, operation)
        return self._audit.call(operation, envelope, self.instance, *arguments)

    def _stateless(self, operation: str, /, *arguments: object) -> Any:  # noqa: ANN401
        return self._access.call(operation, self._envelope(self._access, operation), *arguments)

    # --- instance ------------------------------------------------------------------------------

    def open(self, configuration: Configuration) -> AccessClient:
        instance = self._stateless("open", configuration)
        return AccessClient(self._access, self._audit, self._caller, instance)

    def close(self) -> None:
        self.call("close")

    def initialize(self) -> bool:
        return bool(self.call("initialize"))

    def create_administrator(
        self,
        user_name: str,
        display_name: str,
        *,
        recover: bool = False,
        recovery: str | None = None,
    ) -> NewUser:
        made: NewUser = self.call(
            "create_administrator", user_name, display_name, recover, recovery
        )
        return made

    # --- sign-in and sessions ------------------------------------------------------------------

    def sign_in_settings(self) -> SignInSettings:
        found: SignInSettings = self.call("sign_in_settings")
        return found

    def change_sign_in_settings(
        self,
        *,
        absolute_seconds: int | None = None,
        idle_seconds: int | None = None,
        public_origin: str | None = None,
        trusted_proxies: tuple[str, ...] | None = None,
    ) -> SignInSettings:
        found: SignInSettings = self.call(
            "change_sign_in_settings",
            absolute_seconds,
            idle_seconds,
            public_origin,
            trusted_proxies,
        )
        return found

    def login(
        self, user_name: str, password: str, address: str | None, presented: str | None = None
    ) -> SignedIn:
        signed: SignedIn = self.call("login", user_name, password, address, presented)
        return signed

    def session(self, session_id: str) -> Session:
        live: Session = self.call("session", session_id)
        return live

    def logout(self, session_id: str) -> None:
        self.call("logout", session_id)

    def change_password(self, session_id: str, current: str, new: str, address: str | None) -> str:
        return str(self.call("change_password", session_id, current, new, address))

    def reset_password(self, user: str) -> str:
        return str(self.call("reset_password", user))

    # --- directory -----------------------------------------------------------------------------

    def allowed(self, permission: str, project: str | None) -> bool:
        """Whether the caller may reach an administration route of `permission` (#446)."""
        return bool(self.call("allowed", permission, project))

    def users(self, after: str | None = None) -> UserPage:
        page: UserPage = self.call("users", after)
        return page

    def user(self, user: str) -> User:
        found: User = self.call("user", user)
        return found

    def find_user(self, user_name: str) -> FoundUser:
        found: FoundUser = self.call("find_user", user_name)
        return found

    def members(self, project: str) -> tuple[Member, ...]:
        found: tuple[Member, ...] = self.call("members", project)
        return found

    def groups(self, project: str) -> dict[str, tuple[str, ...]]:
        found: dict[str, tuple[str, ...]] = self.call("groups", project)
        return found

    def attributes(self, project: str, user: str) -> dict[str, Attribute]:
        found: dict[str, Attribute] = self.call("attributes", project, user)
        return found

    def identity_links(self, user: str) -> tuple[IdentityLink, ...]:
        found: tuple[IdentityLink, ...] = self.call("identity_links", user)
        return found

    def create_user(self, user_name: str, display_name: str) -> NewUser:
        made: NewUser = self.call("create_user", user_name, display_name)
        return made

    def deactivate_user(self, user: str) -> None:
        self.call("deactivate_user", user)

    def reactivate_user(self, user: str) -> None:
        self.call("reactivate_user", user)

    def set_administrator(self, user: str, value: bool) -> None:
        self.call("set_administrator", user, value)

    def set_membership(self, project: str, user: str, role: str) -> None:
        self.call("set_membership", project, user, role)

    def remove_membership(self, project: str, user: str) -> None:
        self.call("remove_membership", project, user)

    def create_group(self, project: str, name: str) -> None:
        self.call("create_group", project, name)

    def delete_group(self, project: str, name: str) -> None:
        self.call("delete_group", project, name)

    def add_group_member(self, project: str, group: str, user: str) -> None:
        self.call("add_group_member", project, group, user)

    def remove_group_member(self, project: str, group: str, user: str) -> None:
        self.call("remove_group_member", project, group, user)

    def set_attribute(
        self, project: str, user: str, name: str, kind: str, value: AttributeInput
    ) -> None:
        self.call("set_attribute", project, user, name, kind, value)

    def remove_attribute(self, project: str, user: str, name: str) -> None:
        self.call("remove_attribute", project, user, name)

    def link_identity(self, user: str, provider: str, subject: str) -> str:
        return str(self.call("link_identity", user, provider, subject))

    def unlink_identity(self, user: str, link: str) -> None:
        self.call("unlink_identity", user, link)

    # --- query limits --------------------------------------------------------------------------

    def query_limits(self) -> dict[str, RoleLimit]:
        found: dict[str, RoleLimit] = self.call("query_limits")
        return found

    def change_query_limits(self, limits: dict[str, RoleLimit]) -> dict[str, RoleLimit]:
        found: dict[str, RoleLimit] = self.call("change_query_limits", limits)
        return found

    # --- security context ----------------------------------------------------------------------

    def role_of(self, user: str, project: str) -> str | None:
        found: str | None = self.call("role_of", user, project)
        return found

    def audience_of(self, user: str, project: str) -> Audience:
        found: Audience = self.call("audience_of", user, project)
        return found

    def server_context(
        self, user: str, project: str, policy_fingerprint: str, mapping: dict[str, str]
    ) -> str:
        return str(self.call("server_context", user, project, policy_fingerprint, mapping))

    def local_context(
        self, project: str, authenticated: bool, policy_fingerprint: str, mapping: dict[str, str]
    ) -> str:
        found = self._stateless(
            "local_context", project, authenticated, policy_fingerprint, mapping
        )
        return str(found)

    def fixture_context(self, fixture: ContextFixture, user: str, project: str) -> str:
        return str(self._stateless("fixture_context", fixture, user, project))

    def fixture_may_reuse(
        self, stored: str | None, fixture: ContextFixture, user: str, project: str
    ) -> bool:
        return bool(self._stateless("fixture_may_reuse", stored, fixture, user, project))

    def read_context(self, document: str) -> str:
        return str(self._stateless("read_context", document))

    def permission_fingerprint(self, inputs: FingerprintInputs) -> str:
        return str(self._stateless("permission_fingerprint", inputs))

    # --- OIDC ----------------------------------------------------------------------------------

    def oidc_providers(self) -> tuple[str, ...]:
        found: tuple[str, ...] = self.call("oidc_providers")
        return found

    def oidc_start(self, provider: str, refreshing: str | None = None) -> OidcStarted:
        started: OidcStarted = self.call("oidc_start", provider, refreshing)
        return started

    def oidc_callback(
        self,
        provider: str,
        parameters: dict[str, str],
        binding: str | None,
        presented: str | None = None,
    ) -> SignedIn:
        signed: SignedIn = self.call("oidc_callback", provider, parameters, binding, presented)
        return signed

    def oidc_refresh_due(self, session_id: str) -> str | None:
        found: str | None = self.call("oidc_refresh_due", session_id)
        return found

    # --- the audit log -------------------------------------------------------------------------

    def record(
        self,
        kind: str,
        details: dict[str, str],
        *,
        outcome: Outcome = "allowed",
        project: str | None = None,
    ) -> str:
        return str(self.audit_call("record", kind, details, outcome, project))

    def refusal(
        self, operation: str, code: str, *, project: str | None = None, target: str | None = None
    ) -> None:
        self.audit_call("refusal", operation, code, project, target)

    def count_unrecorded(self) -> int:
        return int(self.audit_call("count_unrecorded"))

    def unrecorded(self) -> int:
        return int(self.audit_call("unrecorded"))

    def events(self) -> tuple[str, ...]:
        found: tuple[str, ...] = self.audit_call("events")
        return found

    def verify(self, anchors: tuple[str, ...] = ()) -> int:
        return int(self.audit_call("verify", anchors))

    def verify_at_start(self) -> int:
        return int(self.audit_call("verify_at_start"))

    def retain(self, retention_days: int) -> tuple[int, int] | None:
        found: tuple[int, int] | None = self.audit_call("retain", retention_days)
        return found

    def rotate_key(self) -> str:
        return str(self.audit_call("rotate_key"))
