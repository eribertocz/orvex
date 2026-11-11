"""OIDC sign-in (application, #439): Authorization Code with PKCE and `response_mode=query`,
ending in an ordinary server-side session (#438); and the silent refresh of a session's
provider groups.

- **Start.** A binding (`bindings.py`) and the provider's authorization URL: `response_type=code`,
  `response_mode=query`, `scope=openid` plus the configured scopes, the S256 challenge, `state`
  and `nonce`; `prompt=none` for a refresh.
- **Callback.**
  - The binding is taken and committed first, then checked.
  - An `error` parameter ends the flow. When the provider advertises it, the `iss` parameter must
    equal the issuer, and one that is present must always equal it (mix-up defense).
  - The code is exchanged with client authentication and the PKCE verifier.
  - The ID token is validated (`tokens.py`); the access token is never used.
  - The identity must be linked to an active user. Then, in one transaction: the mapped groups
    are set, the browser's earlier session ends, and a new one is issued.
- **Refresh.** A provider session's groups are trusted for `group_refresh`; then the next
  request needs a silent re-authentication (`OIDC_REFRESH_REQUIRED`). If it fails for any
  reason, the session ends: it never goes on with stale groups.
- Every failure is the one `OIDC_FAILED` for the browser and `auth.login_failed` (`method: oidc`)
  in the audit log, with one reason from `REASONS`. No token, code, verifier or secret is ever
  recorded.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from orvex.access.audit.log import SYSTEM, AuditLog
from orvex.access.authentication import sessions
from orvex.access.authentication import settings as sign_in_settings
from orvex.access.authentication.service import SignedIn
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.records import User
from orvex.access.errors import OIDC_FAILED, OIDC_NOT_CONFIGURED, AccessError
from orvex.access.oidc import bindings, group_mapping, identities, tokens
from orvex.access.oidc.configuration import Configuration, Provider
from orvex.access.oidc.discovery import (
    Fetcher,
    Keys,
    Metadata,
    checked_metadata,
    discovery_url,
    failed,
)
from orvex.access.storage import retry
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Store, Transaction

REASONS = (
    "state",
    "error_response",
    "issuer",
    "token_exchange",
    "signature",
    "claims",
    "nonce",
    "unlinked",
    "deactivated",
    "network",
    "refresh",
)
_LONGEST_PARAMETER = 4096
METHOD = "oidc"


def callback_path(provider: str) -> str:
    return f"/api/v1/auth/oidc/{provider}/callback"


@dataclass(frozen=True, slots=True)
class Started:
    """Where to send the browser, and the reference for the binding cookie."""

    location: str
    reference: str


@dataclass(frozen=True, slots=True)
class _Known:
    metadata: Metadata
    keys: Keys


class OidcSignIn:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        configuration: Configuration,
        fetcher: Fetcher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._configuration = configuration
        self._fetcher = fetcher
        self._clock = clock
        self._known: dict[str, _Known] = {}

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(self._configuration.providers)

    # --- start-up --------------------------------------------------------------------------

    def start_up(self) -> None:
        """Read and check every provider's discovery document; reconcile the mapping. Any
        failure is `OIDC_NOT_CONFIGURED`, naming the setting."""
        for provider in self._configuration.providers.values():
            self._learn(provider, at_start=True)

        def work(transaction: Transaction) -> None:
            group_mapping.reconcile(transaction, self._audit, self._configuration, self._clock())

        self._write(work)

    def _learn(self, provider: Provider, *, at_start: bool) -> _Known:
        try:
            document = self._fetcher.get(provider, discovery_url(provider.issuer))
        except AccessError as error:
            if at_start:
                raise AccessError(
                    OIDC_NOT_CONFIGURED,
                    "The sign-in provider cannot be reached.",
                    {"setting": f"provider.{provider.name}.issuer"},
                ) from error
            raise
        metadata = checked_metadata(provider, document, at_start=at_start)
        known = _Known(metadata, Keys(provider, metadata, self._fetcher, self._clock))
        self._known[provider.name] = known
        return known

    def _provider(self, name: str) -> tuple[Provider, _Known]:
        provider = self._configuration.provider(name)
        known = self._known.get(name) or self._learn(provider, at_start=False)
        return provider, known

    # --- start ----------------------------------------------------------------------------

    def start(self, name: str, *, refreshing: str | None = None) -> Started:
        """Begin a sign-in; `refreshing` is the session whose groups are to be read again."""
        provider, known = self._provider(name)
        redirect = self._redirect_uri(name)
        now = self._clock()
        held = sessions.session_hash(refreshing) if refreshing is not None else None

        def work(transaction: Transaction) -> tuple[str, bindings.Binding]:
            return bindings.create(transaction, name, now, held)

        reference, binding = self._write(work)
        query = {
            "response_type": "code",
            "response_mode": "query",
            "client_id": provider.client_id,
            "redirect_uri": redirect,
            "scope": " ".join(("openid", *provider.scopes)),
            "state": binding.state,
            "nonce": binding.nonce,
            "code_challenge": binding.challenge,
            "code_challenge_method": "S256",
        }
        if refreshing is not None:
            query["prompt"] = "none"
        endpoint = known.metadata.authorization_endpoint
        joiner = "&" if "?" in endpoint else "?"
        return Started(endpoint + joiner + urlencode(query), reference)

    def _redirect_uri(self, name: str) -> str:
        with self._store.transaction(read_only=True) as transaction:
            origin = sign_in_settings.read(transaction).public_origin
        if origin is None:
            raise AccessError(
                OIDC_NOT_CONFIGURED,
                "The instance's public origin is not set.",
                {"setting": "public_origin"},
            )
        return origin + callback_path(name)

    # --- callback -------------------------------------------------------------------------

    def callback(
        self,
        name: str,
        parameters: Mapping[str, str],
        reference: str | None,
        presented: str | None = None,
    ) -> SignedIn:
        """Finish a sign-in or a refresh; `OIDC_FAILED` for any failed step."""
        provider, known = self._provider(name)
        now = self._clock()
        taken = self._write(lambda transaction: bindings.take(transaction, reference))
        refreshing = taken.binding.refreshing if taken is not None else None
        user: User | None = None
        try:
            binding = bindings.checked(taken, name, parameters.get("state"), now)
            identity = self._identity(provider, known, binding, parameters, now)
            user = self._linked_user(identity)
            return self._open(provider, identity, user, refreshing, presented, now)
        except AccessError as error:
            if error.code != OIDC_FAILED:
                raise
            if isinstance(error, _DeactivatedError):
                user = error.user
            reason = "refresh" if refreshing is not None else str(error.details["reason"])
            self._refused(user, reason, refreshing)
            raise failed(reason) from None

    def _identity(
        self,
        provider: Provider,
        known: _Known,
        binding: bindings.Binding,
        parameters: Mapping[str, str],
        now: datetime,
    ) -> tokens.Identity:
        if "error" in parameters:
            raise failed("error_response")
        issued_by = parameters.get("iss")
        if (known.metadata.iss_parameter and issued_by is None) or (
            issued_by is not None and issued_by != provider.issuer
        ):
            raise failed("issuer")
        code = parameters.get("code")
        if not code or len(code) > _LONGEST_PARAMETER:
            raise failed("token_exchange")
        id_token = self._exchange(provider, known, binding, code)
        return tokens.validated(provider, known.keys, id_token, binding.nonce, now)

    def _exchange(
        self, provider: Provider, known: _Known, binding: bindings.Binding, code: str
    ) -> str:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri(provider.name),
            "code_verifier": binding.verifier,
            "client_id": provider.client_id,
        }
        try:
            answer = self._fetcher.post_form(
                provider,
                known.metadata.token_endpoint,
                form,
                (provider.client_id, provider.client_secret),
            )
        except AccessError as error:
            reason = error.details.get("reason")
            raise failed("token_exchange" if reason == "token_exchange" else "network") from error
        try:
            found = json.loads(answer)
        except (ValueError, UnicodeDecodeError) as error:
            raise failed("token_exchange") from error
        token = found.get("id_token") if isinstance(found, dict) else None
        if not isinstance(token, str):
            raise failed("token_exchange")
        return token

    def _linked_user(self, identity: tokens.Identity) -> User:
        with self._store.transaction(read_only=True) as transaction:
            user_id = identities.linked_user(transaction, identity.issuer, identity.subject)
            user = None if user_id is None else directory_sql.user(transaction, user_id)
        if user is None:
            raise failed("unlinked")
        if not user.active:
            raise _DeactivatedError(user)
        return user

    def _open(
        self,
        provider: Provider,
        identity: tokens.Identity,
        user: User,
        refreshing: str | None,
        presented: str | None,
        now: datetime,
    ) -> SignedIn:
        rules = self._configuration.rules_for(provider.name)

        def work(transaction: Transaction) -> SignedIn:
            current = directory_sql.user(transaction, user.id)
            if current is None or not current.active:
                raise failed("deactivated")
            if refreshing is not None and not _same_holder(transaction, refreshing, identity):
                raise failed("refresh")
            group_mapping.apply(transaction, self._audit, rules, user.id, identity.claims, now)
            if refreshing is not None:
                transaction.run("DELETE FROM sessions WHERE id_hash = :id", id=refreshing)
            if presented is not None:
                sessions.end(transaction, presented)
            fresh = directory_sql.user(transaction, user.id) or current  # after any bump
            session_id = sessions.issue(transaction, fresh, now, password_change_only=False)
            transaction.run(
                "INSERT INTO oidc_sessions (session, provider, issuer, subject, refreshed_at) "
                "VALUES (:session, :provider, :issuer, :subject, :now)",
                session=sessions.session_hash(session_id),
                provider=provider.name,
                issuer=identity.issuer,
                subject=identity.subject,
                now=instant(now),
            )
            self._audit.record(transaction, "auth.login", fresh.id, {"method": METHOD})
            return SignedIn(session_id, fresh, False)

        return self._write(work)

    def _refused(self, user: User | None, reason: str, refreshing: str | None) -> None:
        def work(transaction: Transaction) -> None:
            if refreshing is not None:
                transaction.run("DELETE FROM sessions WHERE id_hash = :id", id=refreshing)
            self._audit.record(
                transaction,
                "auth.login_failed",
                user.id if user is not None else SYSTEM,
                {"method": METHOD, "reason": reason.upper()},
                outcome="refused",
            )

        self._write(work)

    # --- refresh --------------------------------------------------------------------------

    def refresh_due(self, session_id: str) -> str | None:
        """The provider whose groups a live session must read again now, or None (not a
        provider session, or not due). A provider no longer configured is always due."""
        with self._store.transaction(read_only=True) as transaction:
            found = transaction.rows(
                "SELECT provider, refreshed_at FROM oidc_sessions WHERE session = :session",
                session=sessions.session_hash(session_id),
            )
        if not found:
            return None
        name, refreshed = str(found[0][0]), parsed(str(found[0][1]))
        provider = self._configuration.providers.get(name)
        if provider is None or self._clock() >= refreshed + provider.group_refresh:
            return name
        return None

    def _write[T](self, work: Callable[[Transaction], T]) -> T:
        try:
            done = retry.write(self._store, work)
        except AccessError as error:
            if error.code.startswith("AUDIT_"):
                self._audit.unavailable()
            raise
        self._audit.committed()
        return done


class _DeactivatedError(AccessError):
    """A linked user who is deactivated: refused as `deactivated`, naming the user in the log."""

    def __init__(self, user: User) -> None:
        super().__init__(OIDC_FAILED, "Sign-in failed.", {"reason": "deactivated"})
        self.user = user


def _same_holder(transaction: Transaction, held: str, identity: tokens.Identity) -> bool:
    found = transaction.rows(
        "SELECT issuer, subject FROM oidc_sessions WHERE session = :session", session=held
    )
    return bool(found) and (str(found[0][0]), str(found[0][1])) == (
        identity.issuer,
        identity.subject,
    )
