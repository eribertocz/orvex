"""The interface contract of `access` (#486 §5 step 4): sign-in, sessions, the directory, the
security context, the instance settings and OIDC, as every consumer uses them.

Each operation takes the call envelope first; the envelope's `actor` is the effective actor of
the operation (the signed-in user, `anonymous` before a sign-in, `system` for the instance's
own work). Every operation but `open` acts on an `Instance` the implementation opened. The
audit log is the `audit-log` contract (`orvex.contracts.audit_log`), served by the same
implementation on the same instances.
"""

from __future__ import annotations

from typing import Protocol

from orvex.contracts.access.errors import AccessError
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
    RoleLimit,
    Session,
    SignedIn,
    SignInSettings,
    UserPage,
)
from orvex.contracts.access.values import User as DirectoryUser
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.version import Version

ANONYMOUS = "anonymous"
SYSTEM = "system"


class Access(Protocol):
    # --- instance ------------------------------------------------------------------------------
    def open(self, envelope: CallEnvelope, configuration: Configuration) -> Instance: ...

    def close(self, envelope: CallEnvelope, instance: Instance) -> None: ...

    def initialize(self, envelope: CallEnvelope, instance: Instance) -> bool: ...

    def create_administrator(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        user_name: str,
        display_name: str,
        recover: bool,
        recovery_file: str | None,
    ) -> NewUser: ...

    # --- sign-in and sessions ------------------------------------------------------------------
    def sign_in_settings(self, envelope: CallEnvelope, instance: Instance) -> SignInSettings: ...

    def change_sign_in_settings(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        absolute_seconds: int | None,
        idle_seconds: int | None,
        public_origin: str | None,
        trusted_proxies: tuple[str, ...] | None,
    ) -> SignInSettings: ...

    def login(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        user_name: str,
        password: str,
        address: str | None,
        presented: str | None,
    ) -> SignedIn: ...

    def session(self, envelope: CallEnvelope, instance: Instance, session_id: str) -> Session: ...

    def logout(self, envelope: CallEnvelope, instance: Instance, session_id: str) -> None: ...

    def change_password(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        session_id: str,
        current: str,
        new: str,
        address: str | None,
    ) -> str: ...

    def reset_password(self, envelope: CallEnvelope, instance: Instance, user: str) -> str: ...

    # --- directory reads -----------------------------------------------------------------------
    def users(self, envelope: CallEnvelope, instance: Instance, after: str | None) -> UserPage: ...

    def user(self, envelope: CallEnvelope, instance: Instance, user: str) -> DirectoryUser: ...

    def find_user(
        self, envelope: CallEnvelope, instance: Instance, user_name: str
    ) -> FoundUser: ...

    def members(
        self, envelope: CallEnvelope, instance: Instance, project: str
    ) -> tuple[Member, ...]: ...

    def groups(
        self, envelope: CallEnvelope, instance: Instance, project: str
    ) -> dict[str, tuple[str, ...]]: ...

    def attributes(
        self, envelope: CallEnvelope, instance: Instance, project: str, user: str
    ) -> dict[str, Attribute]: ...

    def identity_links(
        self, envelope: CallEnvelope, instance: Instance, user: str
    ) -> tuple[IdentityLink, ...]: ...

    # --- directory changes ---------------------------------------------------------------------
    def create_user(
        self, envelope: CallEnvelope, instance: Instance, user_name: str, display_name: str
    ) -> NewUser: ...

    def deactivate_user(self, envelope: CallEnvelope, instance: Instance, user: str) -> None: ...

    def reactivate_user(self, envelope: CallEnvelope, instance: Instance, user: str) -> None: ...

    def set_administrator(
        self, envelope: CallEnvelope, instance: Instance, user: str, value: bool
    ) -> None: ...

    def set_membership(
        self, envelope: CallEnvelope, instance: Instance, project: str, user: str, role: str
    ) -> None: ...

    def remove_membership(
        self, envelope: CallEnvelope, instance: Instance, project: str, user: str
    ) -> None: ...

    def create_group(
        self, envelope: CallEnvelope, instance: Instance, project: str, name: str
    ) -> None: ...

    def delete_group(
        self, envelope: CallEnvelope, instance: Instance, project: str, name: str
    ) -> None: ...

    def add_group_member(
        self, envelope: CallEnvelope, instance: Instance, project: str, group: str, user: str
    ) -> None: ...

    def remove_group_member(
        self, envelope: CallEnvelope, instance: Instance, project: str, group: str, user: str
    ) -> None: ...

    def set_attribute(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        project: str,
        user: str,
        name: str,
        kind: str,
        value: AttributeInput,
    ) -> None: ...

    def remove_attribute(
        self, envelope: CallEnvelope, instance: Instance, project: str, user: str, name: str
    ) -> None: ...

    def link_identity(
        self, envelope: CallEnvelope, instance: Instance, user: str, provider: str, subject: str
    ) -> str: ...

    def unlink_identity(
        self, envelope: CallEnvelope, instance: Instance, user: str, link: str
    ) -> None: ...

    # --- query limits --------------------------------------------------------------------------
    def query_limits(self, envelope: CallEnvelope, instance: Instance) -> dict[str, RoleLimit]: ...

    def change_query_limits(
        self, envelope: CallEnvelope, instance: Instance, limits: dict[str, RoleLimit]
    ) -> dict[str, RoleLimit]: ...

    # --- security context ----------------------------------------------------------------------
    def role_of(
        self, envelope: CallEnvelope, instance: Instance, user: str, project: str
    ) -> str | None: ...

    def audience_of(
        self, envelope: CallEnvelope, instance: Instance, user: str, project: str
    ) -> Audience: ...

    def server_context(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        user: str,
        project: str,
        policy_fingerprint: str,
        mapping_snapshots: dict[str, str],
    ) -> str: ...

    def local_context(
        self,
        envelope: CallEnvelope,
        project: str,
        authenticated: bool,
        policy_fingerprint: str,
        mapping_snapshots: dict[str, str],
    ) -> str: ...

    def fixture_context(
        self, envelope: CallEnvelope, fixture: ContextFixture, user: str, project: str
    ) -> str: ...

    def fixture_may_reuse(
        self,
        envelope: CallEnvelope,
        stored_fingerprint: str | None,
        fixture: ContextFixture,
        user: str,
        project: str,
    ) -> bool: ...

    def read_context(self, envelope: CallEnvelope, document: str) -> str: ...

    def permission_fingerprint(self, envelope: CallEnvelope, inputs: FingerprintInputs) -> str: ...

    # --- OIDC ----------------------------------------------------------------------------------
    def oidc_providers(self, envelope: CallEnvelope, instance: Instance) -> tuple[str, ...]: ...

    def oidc_start(
        self, envelope: CallEnvelope, instance: Instance, provider: str, refreshing: str | None
    ) -> OidcStarted: ...

    def oidc_callback(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        provider: str,
        parameters: dict[str, str],
        binding: str | None,
        presented: str | None,
    ) -> SignedIn: ...

    def oidc_refresh_due(
        self, envelope: CallEnvelope, instance: Instance, session_id: str
    ) -> str | None: ...


# Codes by what refuses them.
_INSTANCE = ("ACCESS_INSTANCE_UNKNOWN",)
_STORE = ("ACCESS_STORE_NEWER", "ACCESS_STORE_UNSAFE", "VALIDATION_FAILED")
_AUDIT = ("AUDIT_UNAVAILABLE", "AUDIT_EVENT_INVALID", "AUDIT_KEY_UNSAFE")
_OPEN = (
    *_INSTANCE,
    *_STORE,
    *_AUDIT,
    "AUDIT_CHAIN_BROKEN",
    "AUDIT_CHECKPOINT_INVALID",
    "OIDC_NOT_CONFIGURED",
)
_WRITE = (*_INSTANCE, *_AUDIT, "ACCESS_FORBIDDEN", "VALIDATION_FAILED")
_READ = (*_INSTANCE, *_AUDIT, "ACCESS_FORBIDDEN")
_USER = ("USER_NOT_FOUND",)
_PROJECT = ("PROJECT_NOT_SERVED",)
_SESSION = ("SESSION_EXPIRED", "PASSWORD_CHANGE_REQUIRED", "OIDC_REFRESH_REQUIRED")
_SIGN_IN = (*_INSTANCE, *_AUDIT, "LOGIN_FAILED", "LOGIN_DELAYED", "VALIDATION_FAILED")
_OIDC = (*_INSTANCE, *_AUDIT, "OIDC_FAILED", "OIDC_NOT_CONFIGURED", "VALIDATION_FAILED")
_CONTEXT = ("CONTEXT_INVALID", "VALIDATION_FAILED")
_MEMBERS = (*_WRITE, *_USER, *_PROJECT, "LAST_BUILDER")
_GROUPS = (*_WRITE, *_USER, *_PROJECT, "GROUP_NOT_FOUND", "USER_NAME_TAKEN")


def _codes(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


ERRORS = _codes(
    _OPEN,
    _WRITE,
    _SESSION,
    _SIGN_IN,
    _OIDC,
    _CONTEXT,
    _MEMBERS,
    _GROUPS,
    (
        "LAST_ADMINISTRATOR",
        "USER_NAME_TAKEN",
        "IDENTITY_NOT_FOUND",
        "IDENTITY_TAKEN",
        "LOOKUP_LIMITED",
        "PASSWORD_REJECTED",
        "AUDIT_RETENTION_INVALID",
    ),
)

ACCESS = InterfaceContract(
    name="access",
    version=Version(1, 0),
    protocol=Access,
    error_type=AccessError,
    capabilities=("instance", "sign-in", "directory", "limits", "context", "oidc"),
    errors=ERRORS,
    operations=(
        Operation("open", "instance", _OPEN, changes_state=True),
        Operation("close", "instance", _INSTANCE, changes_state=True),
        Operation("initialize", "instance", _OPEN, changes_state=True),
        Operation(
            "create_administrator",
            "instance",
            _codes(_OPEN, _WRITE, ("USER_NAME_TAKEN", "PASSWORD_REJECTED")),
            changes_state=True,
        ),
        Operation("sign_in_settings", "sign-in", _READ),
        Operation("change_sign_in_settings", "sign-in", _WRITE, changes_state=True),
        Operation("login", "sign-in", _SIGN_IN, changes_state=True),
        Operation("session", "sign-in", _codes(_READ, _SESSION)),
        Operation("logout", "sign-in", _READ, changes_state=True),
        Operation(
            "change_password",
            "sign-in",
            _codes(_SIGN_IN, _SESSION, ("PASSWORD_REJECTED",)),
            changes_state=True,
        ),
        Operation("reset_password", "sign-in", _codes(_WRITE, _USER), changes_state=True),
        Operation("users", "directory", _codes(_READ, ("VALIDATION_FAILED",))),
        Operation("user", "directory", _codes(_READ, _USER)),
        Operation("find_user", "directory", _codes(_READ, _USER, ("LOOKUP_LIMITED",))),
        Operation("members", "directory", _codes(_READ, _PROJECT)),
        Operation("groups", "directory", _codes(_READ, _PROJECT)),
        Operation("attributes", "directory", _codes(_READ, _USER, _PROJECT)),
        Operation("identity_links", "directory", _codes(_READ, _USER)),
        Operation(
            "create_user", "directory", _codes(_WRITE, ("USER_NAME_TAKEN",)), changes_state=True
        ),
        Operation(
            "deactivate_user",
            "directory",
            _codes(_WRITE, _USER, ("LAST_ADMINISTRATOR", "LAST_BUILDER")),
            changes_state=True,
        ),
        Operation("reactivate_user", "directory", _codes(_WRITE, _USER), changes_state=True),
        Operation(
            "set_administrator",
            "directory",
            _codes(_WRITE, _USER, ("LAST_ADMINISTRATOR",)),
            changes_state=True,
        ),
        Operation("set_membership", "directory", _MEMBERS, changes_state=True),
        Operation("remove_membership", "directory", _MEMBERS, changes_state=True),
        Operation("create_group", "directory", _GROUPS, changes_state=True),
        Operation("delete_group", "directory", _GROUPS, changes_state=True),
        Operation("add_group_member", "directory", _GROUPS, changes_state=True),
        Operation("remove_group_member", "directory", _GROUPS, changes_state=True),
        Operation("set_attribute", "directory", _MEMBERS, changes_state=True),
        Operation("remove_attribute", "directory", _MEMBERS, changes_state=True),
        Operation(
            "link_identity",
            "directory",
            _codes(_WRITE, _USER, ("IDENTITY_TAKEN", "OIDC_NOT_CONFIGURED")),
            changes_state=True,
        ),
        Operation(
            "unlink_identity",
            "directory",
            _codes(_WRITE, _USER, ("IDENTITY_NOT_FOUND",)),
            changes_state=True,
        ),
        Operation("query_limits", "limits", _READ),
        Operation("change_query_limits", "limits", _WRITE, changes_state=True),
        Operation("role_of", "context", _READ),
        Operation("audience_of", "context", _READ),
        Operation("server_context", "context", _codes(_READ, _CONTEXT)),
        Operation("local_context", "context", _CONTEXT),
        Operation("fixture_context", "context", _CONTEXT),
        Operation("fixture_may_reuse", "context", _CONTEXT),
        Operation("read_context", "context", _CONTEXT),
        Operation("permission_fingerprint", "context", _CONTEXT),
        Operation("oidc_providers", "oidc", _INSTANCE),
        Operation("oidc_start", "oidc", _codes(_OIDC, _SESSION), changes_state=True),
        Operation("oidc_callback", "oidc", _OIDC, changes_state=True),
        Operation("oidc_refresh_due", "oidc", _READ),
    ),
)
