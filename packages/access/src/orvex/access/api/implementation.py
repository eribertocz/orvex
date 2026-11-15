"""`Access`, the implementation of the `access` and `audit-log` interface contracts: every
operation delegates to the module's own services on an opened instance.

- **Instances** are opened by `open` and named by a random handle; any other handle, or a closed
  one, is `ACCESS_INSTANCE_UNKNOWN` and nothing is done.
- **The actor** of every operation is the envelope's: the directory's rules decide what that
  actor may do, exactly as before (one enforcement point).
- **Idempotency.** Every operation that changes state runs once per idempotency key
  (`idempotency.py`).
- **Composition.** The composer may give a clock and the server's provider fetcher
  (`orvex.contracts.access.fetcher`); without a fetcher, an instance with OIDC providers is not
  opened.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orvex.access import server_context
from orvex.access.api import contexts, idempotency
from orvex.access.api import values as convert
from orvex.access.api.instances import Opened, open_instance
from orvex.access.audit.rotation import rotate_key
from orvex.access.oidc.service import OidcSignIn
from orvex.contracts.access import values
from orvex.contracts.access.errors import (
    ACCESS_INSTANCE_UNKNOWN,
    OIDC_NOT_CONFIGURED,
    AccessError,
)
from orvex.contracts.access.fetcher import ProviderFetcher
from orvex.contracts.envelope import CallEnvelope


def _now() -> datetime:
    return datetime.now(UTC)


class Access:
    """Both contracts' operations over the instances this object opened."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        fetcher: ProviderFetcher | None = None,
    ) -> None:
        self._clock = _now if clock is None else clock
        self._fetcher = fetcher
        self._opened: dict[str, tuple[Opened, values.Configuration]] = {}
        self._lock = threading.Lock()
        self._replays = idempotency.Replays()

    # --- the machinery ---------------------------------------------------------------------

    def _of(self, instance: values.Instance) -> Opened:
        with self._lock:
            found = self._opened.get(instance.id)
        if found is None:
            raise AccessError(ACCESS_INSTANCE_UNKNOWN, "No such access instance.")
        return found[0]

    def _configuration(self, instance: values.Instance) -> values.Configuration:
        self._of(instance)
        with self._lock:
            return self._opened[instance.id][1]

    def _once[T](
        self,
        operation: str,
        envelope: CallEnvelope,
        arguments: tuple[object, ...],
        work: Callable[[], T],
    ) -> T:
        return self._replays.once(operation, envelope, arguments, work)

    def _oidc(self, instance: values.Instance) -> OidcSignIn:
        oidc = self._of(instance).oidc
        if oidc is None:
            raise AccessError(OIDC_NOT_CONFIGURED, "No such sign-in provider.")
        return oidc

    # --- instance --------------------------------------------------------------------------

    def open(self, envelope: CallEnvelope, configuration: values.Configuration) -> values.Instance:
        def work() -> values.Instance:
            opened = open_instance(configuration, self._clock, self._fetcher)
            handle = values.Instance(secrets.token_urlsafe(16))
            with self._lock:
                self._opened[handle.id] = (opened, configuration)
            return handle

        return self._once("open", envelope, (configuration,), work)

    def close(self, envelope: CallEnvelope, instance: values.Instance) -> None:
        def work() -> None:
            with self._lock:
                found = self._opened.pop(instance.id, None)
            if found is None:
                raise AccessError(ACCESS_INSTANCE_UNKNOWN, "No such access instance.")
            found[0].store.close()

        self._once("close", envelope, (instance,), work)

    def initialize(self, envelope: CallEnvelope, instance: values.Instance) -> bool:
        return self._once(
            "initialize", envelope, (instance,), lambda: self._of(instance).bootstrap.initialize()
        )

    def create_administrator(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        user_name: str,
        display_name: str,
        recover: bool,
        recovery_file: str | None,
    ) -> values.NewUser:
        arguments = (instance, user_name, display_name, recover, recovery_file)
        recovery = None if recovery_file is None else Path(recovery_file)
        return self._once(
            "create_administrator",
            envelope,
            arguments,
            lambda: convert.new_user(
                self._of(instance).bootstrap.create_administrator(
                    user_name, display_name, recover=recover, recovery=recovery
                )
            ),
        )

    # --- sign-in and sessions --------------------------------------------------------------

    def sign_in_settings(
        self, envelope: CallEnvelope, instance: values.Instance
    ) -> values.SignInSettings:
        return convert.settings(self._of(instance).settings.current())

    def change_sign_in_settings(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        absolute_seconds: int | None,
        idle_seconds: int | None,
        public_origin: str | None,
        trusted_proxies: tuple[str, ...] | None,
    ) -> values.SignInSettings:
        def work() -> values.SignInSettings:
            return convert.settings(
                self._of(instance).settings.change(
                    envelope.actor,
                    absolute=None
                    if absolute_seconds is None
                    else timedelta(seconds=absolute_seconds),
                    idle=None if idle_seconds is None else timedelta(seconds=idle_seconds),
                    public_origin=public_origin,
                    trusted_proxies=trusted_proxies,
                )
            )

        arguments = (instance, absolute_seconds, idle_seconds, public_origin, trusted_proxies)
        return self._once("change_sign_in_settings", envelope, arguments, work)

    def login(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        user_name: str,
        password: str,
        address: str | None,
        presented: str | None,
    ) -> values.SignedIn:
        return self._once(
            "login",
            envelope,
            (instance, user_name, password, address, presented),
            lambda: convert.signed_in(
                self._of(instance).sign_in.login(user_name, password, address, presented)
            ),
        )

    def session(
        self, envelope: CallEnvelope, instance: values.Instance, session_id: str
    ) -> values.Session:
        return convert.session(self._of(instance).sign_in.session(session_id))

    def logout(self, envelope: CallEnvelope, instance: values.Instance, session_id: str) -> None:
        self._once(
            "logout",
            envelope,
            (instance, session_id),
            lambda: self._of(instance).sign_in.logout(session_id),
        )

    def change_password(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        session_id: str,
        current: str,
        new: str,
        address: str | None,
    ) -> str:
        return self._once(
            "change_password",
            envelope,
            (instance, session_id, current, new, address),
            lambda: self._of(instance).sign_in.change_password(session_id, current, new, address),
        )

    def reset_password(self, envelope: CallEnvelope, instance: values.Instance, user: str) -> str:
        return self._once(
            "reset_password",
            envelope,
            (instance, user),
            lambda: self._of(instance).sign_in.reset_password(envelope.actor, user),
        )

    # --- directory reads -------------------------------------------------------------------

    def users(
        self, envelope: CallEnvelope, instance: values.Instance, after: str | None
    ) -> values.UserPage:
        return convert.page(self._of(instance).reads.users(envelope.actor, after))

    def user(self, envelope: CallEnvelope, instance: values.Instance, user: str) -> values.User:
        return convert.user(self._of(instance).reads.user(envelope.actor, user))

    def find_user(
        self, envelope: CallEnvelope, instance: values.Instance, user_name: str
    ) -> values.FoundUser:
        return convert.found_user(self._of(instance).reads.find(envelope.actor, user_name))

    def members(
        self, envelope: CallEnvelope, instance: values.Instance, project: str
    ) -> tuple[values.Member, ...]:
        return convert.members(self._of(instance).reads.members(envelope.actor, project))

    def groups(
        self, envelope: CallEnvelope, instance: values.Instance, project: str
    ) -> dict[str, tuple[str, ...]]:
        return convert.groups(self._of(instance).reads.groups(envelope.actor, project))

    def attributes(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, user: str
    ) -> dict[str, values.Attribute]:
        found = self._of(instance).reads.attributes(envelope.actor, project, user)
        return convert.attributes(found)

    def identity_links(
        self, envelope: CallEnvelope, instance: values.Instance, user: str
    ) -> tuple[values.IdentityLink, ...]:
        return convert.links(self._of(instance).identities.links(envelope.actor, user))

    # --- directory changes -----------------------------------------------------------------

    def create_user(
        self, envelope: CallEnvelope, instance: values.Instance, user_name: str, display_name: str
    ) -> values.NewUser:
        return self._once(
            "create_user",
            envelope,
            (instance, user_name, display_name),
            lambda: convert.new_user(
                self._of(instance).directory.create_user(envelope.actor, user_name, display_name)
            ),
        )

    def _change(
        self,
        operation: str,
        envelope: CallEnvelope,
        instance: values.Instance,
        arguments: tuple[object, ...],
        work: Callable[[Opened], object],
    ) -> None:
        self._once(operation, envelope, (instance, *arguments), lambda: work(self._of(instance)))

    def deactivate_user(self, envelope: CallEnvelope, instance: values.Instance, user: str) -> None:
        self._change(
            "deactivate_user",
            envelope,
            instance,
            (user,),
            lambda opened: opened.directory.deactivate_user(envelope.actor, user),
        )

    def reactivate_user(self, envelope: CallEnvelope, instance: values.Instance, user: str) -> None:
        self._change(
            "reactivate_user",
            envelope,
            instance,
            (user,),
            lambda opened: opened.directory.reactivate_user(envelope.actor, user),
        )

    def set_administrator(
        self, envelope: CallEnvelope, instance: values.Instance, user: str, value: bool
    ) -> None:
        self._change(
            "set_administrator",
            envelope,
            instance,
            (user, value),
            lambda opened: opened.directory.set_administrator(envelope.actor, user, value),
        )

    def set_membership(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, user: str, role: str
    ) -> None:
        self._change(
            "set_membership",
            envelope,
            instance,
            (project, user, role),
            lambda opened: opened.directory.set_membership(envelope.actor, project, user, role),
        )

    def remove_membership(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, user: str
    ) -> None:
        self._change(
            "remove_membership",
            envelope,
            instance,
            (project, user),
            lambda opened: opened.directory.remove_membership(envelope.actor, project, user),
        )

    def create_group(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, name: str
    ) -> None:
        self._change(
            "create_group",
            envelope,
            instance,
            (project, name),
            lambda opened: opened.directory.create_group(envelope.actor, project, name),
        )

    def delete_group(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, name: str
    ) -> None:
        self._change(
            "delete_group",
            envelope,
            instance,
            (project, name),
            lambda opened: opened.directory.delete_group(envelope.actor, project, name),
        )

    def add_group_member(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, group: str, user: str
    ) -> None:
        self._change(
            "add_group_member",
            envelope,
            instance,
            (project, group, user),
            lambda opened: opened.directory.add_group_member(envelope.actor, project, group, user),
        )

    def remove_group_member(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, group: str, user: str
    ) -> None:
        self._change(
            "remove_group_member",
            envelope,
            instance,
            (project, group, user),
            lambda opened: opened.directory.remove_group_member(
                envelope.actor, project, group, user
            ),
        )

    def set_attribute(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        project: str,
        user: str,
        name: str,
        kind: str,
        value: values.AttributeInput,
    ) -> None:
        self._change(
            "set_attribute",
            envelope,
            instance,
            (project, user, name, kind, value),
            lambda opened: opened.attributes.set(envelope.actor, project, user, name, kind, value),
        )

    def remove_attribute(
        self, envelope: CallEnvelope, instance: values.Instance, project: str, user: str, name: str
    ) -> None:
        self._change(
            "remove_attribute",
            envelope,
            instance,
            (project, user, name),
            lambda opened: opened.attributes.remove(envelope.actor, project, user, name),
        )

    def link_identity(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        user: str,
        provider: str,
        subject: str,
    ) -> str:
        return self._once(
            "link_identity",
            envelope,
            (instance, user, provider, subject),
            lambda: self._of(instance).identities.link(envelope.actor, user, provider, subject),
        )

    def unlink_identity(
        self, envelope: CallEnvelope, instance: values.Instance, user: str, link: str
    ) -> None:
        self._change(
            "unlink_identity",
            envelope,
            instance,
            (user, link),
            lambda opened: opened.identities.unlink(envelope.actor, user, link),
        )

    # --- query limits ----------------------------------------------------------------------

    def query_limits(
        self, envelope: CallEnvelope, instance: values.Instance
    ) -> dict[str, values.RoleLimit]:
        return convert.limits(self._of(instance).limits.current())

    def change_query_limits(
        self, envelope: CallEnvelope, instance: values.Instance, limits: dict[str, values.RoleLimit]
    ) -> dict[str, values.RoleLimit]:
        return self._once(
            "change_query_limits",
            envelope,
            (instance, limits),
            lambda: convert.limits(
                self._of(instance).limits.change(envelope.actor, convert.role_limits(limits))
            ),
        )

    # --- security context ------------------------------------------------------------------

    def role_of(
        self, envelope: CallEnvelope, instance: values.Instance, user: str, project: str
    ) -> str | None:
        request = server_context.ServerRequest(user, project)
        return server_context.role_of(self._of(instance).store, request)

    def audience_of(
        self, envelope: CallEnvelope, instance: values.Instance, user: str, project: str
    ) -> values.Audience:
        request = server_context.ServerRequest(user, project)
        return server_context.audience_of(self._of(instance).store, request)

    def server_context(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        user: str,
        project: str,
        policy_fingerprint: str,
        mapping_snapshots: dict[str, str],
    ) -> str:
        opened = self._of(instance)
        provider = server_context.ServerContextProvider(
            opened.store, opened.audit, policy_fingerprint, mapping_snapshots
        )
        request = server_context.ServerRequest(user, project)
        return contexts.text(provider.context_for(request).document())

    def local_context(
        self,
        envelope: CallEnvelope,
        project: str,
        authenticated: bool,
        policy_fingerprint: str,
        mapping_snapshots: dict[str, str],
    ) -> str:
        return contexts.local_context(project, authenticated, policy_fingerprint, mapping_snapshots)

    def fixture_context(
        self, envelope: CallEnvelope, fixture: values.ContextFixture, user: str, project: str
    ) -> str:
        return contexts.fixture_context(fixture, user, project)

    def fixture_may_reuse(
        self,
        envelope: CallEnvelope,
        stored_fingerprint: str | None,
        fixture: values.ContextFixture,
        user: str,
        project: str,
    ) -> bool:
        return contexts.fixture_may_reuse(stored_fingerprint, fixture, user, project)

    def read_context(self, envelope: CallEnvelope, document: str) -> str:
        return contexts.read_context(document)

    def permission_fingerprint(
        self, envelope: CallEnvelope, inputs: values.FingerprintInputs
    ) -> str:
        return contexts.permission_fingerprint(inputs)

    # --- OIDC ------------------------------------------------------------------------------

    def oidc_providers(self, envelope: CallEnvelope, instance: values.Instance) -> tuple[str, ...]:
        oidc = self._of(instance).oidc
        return () if oidc is None else tuple(sorted(oidc.providers))

    def oidc_start(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        provider: str,
        refreshing: str | None,
    ) -> values.OidcStarted:
        return self._once(
            "oidc_start",
            envelope,
            (instance, provider, refreshing),
            lambda: convert.started(self._oidc(instance).start(provider, refreshing=refreshing)),
        )

    def oidc_callback(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        provider: str,
        parameters: dict[str, str],
        binding: str | None,
        presented: str | None,
    ) -> values.SignedIn:
        return self._once(
            "oidc_callback",
            envelope,
            (instance, provider, parameters, binding, presented),
            lambda: convert.signed_in(
                self._oidc(instance).callback(provider, parameters, binding, presented)
            ),
        )

    def oidc_refresh_due(
        self, envelope: CallEnvelope, instance: values.Instance, session_id: str
    ) -> str | None:
        oidc = self._of(instance).oidc
        return None if oidc is None else oidc.refresh_due(session_id)

    # --- the audit log ---------------------------------------------------------------------

    def record(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        kind: str,
        details: dict[str, str],
        outcome: values.Outcome,
        project: str | None,
    ) -> str:
        return self._once(
            "record",
            envelope,
            (instance, kind, details, outcome, project),
            lambda: self._of(instance).audit.record_alone(
                kind, envelope.actor, details, outcome=outcome, project=project
            ),
        )

    def refusal(
        self,
        envelope: CallEnvelope,
        instance: values.Instance,
        operation: str,
        code: str,
        project: str | None,
        target: str | None,
    ) -> None:
        self._change(
            "refusal",
            envelope,
            instance,
            (operation, code, project, target),
            lambda opened: opened.audit.refusal(
                operation, envelope.actor, code, project=project, target=target
            ),
        )

    def count_unrecorded(self, envelope: CallEnvelope, instance: values.Instance) -> int:
        def work() -> int:
            audit = self._of(instance).audit
            with self._lock:
                audit.unrecorded += 1
                return audit.unrecorded

        return self._once("count_unrecorded", envelope, (instance,), work)

    def unrecorded(self, envelope: CallEnvelope, instance: values.Instance) -> int:
        return self._of(instance).audit.unrecorded

    def events(self, envelope: CallEnvelope, instance: values.Instance) -> tuple[str, ...]:
        return tuple(self._of(instance).audit.documents())

    def verify(
        self, envelope: CallEnvelope, instance: values.Instance, anchors: tuple[str, ...]
    ) -> int:
        return self._of(instance).audit.verify(anchors)

    def verify_at_start(self, envelope: CallEnvelope, instance: values.Instance) -> int:
        return self._of(instance).audit.verify_at_start()

    def retain(
        self, envelope: CallEnvelope, instance: values.Instance, retention_days: int
    ) -> tuple[int, int] | None:
        return self._once(
            "retain",
            envelope,
            (instance, retention_days),
            lambda: self._of(instance).audit.retain(retention_days),
        )

    def rotate_key(self, envelope: CallEnvelope, instance: values.Instance) -> str:
        def work() -> str:
            audit = self._of(instance).audit
            key = Path(self._configuration(instance).audit_key)
            return rotate_key(audit, key, envelope.actor)

        return self._once("rotate_key", envelope, (instance,), work)
