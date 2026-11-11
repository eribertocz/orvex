"""Signing in with a password, and the session it opens (application, #438).

- **Sign-in.** The delay is checked and the attempt reserved first (`delays.py`); the password
  is checked outside any transaction; the outcome is then written with its audit event. A wrong
  password, an unknown user, a deactivated user, an expired temporary password and a user
  without a password all get `LOGIN_FAILED`, after the same Argon2id work. The audit log names
  which (`auth.login_failed`).
- **The new session.** Signing in always issues a new id and ends the one the browser presented.
  A session opened with a temporary password may only change it.
- **Every request** re-reads the user (`sessions.py`): an ended session is `SESSION_EXPIRED`.
- **Password change.** The current password is checked under the same delay as a sign-in. Every
  session of the user ends, and the one that made the change gets a new id.
- **Reset.** An administrator sets a temporary password, shown once, and every session of the
  user ends.
- **Provider groups** (#439) are never carried into a password session: signing in with a
  password takes back every group an OIDC mapping granted. A password change in a provider
  session keeps it a provider session, so its groups are still refreshed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from orvex.access.audit.log import SYSTEM, AuditLog
from orvex.access.audit.operation import audited
from orvex.access.authentication import credentials, delays, passwords, sessions
from orvex.access.authentication import settings as sign_in_settings
from orvex.access.authentication.credentials import Credential
from orvex.access.authentication.sessions import NEEDS_WRITE, Ended, Session
from orvex.access.directory import rules
from orvex.access.directory import statements as directory_sql
from orvex.access.directory.names import canonical_user_name
from orvex.access.directory.records import User
from orvex.access.directory.rules import refused
from orvex.access.errors import (
    LOGIN_DELAYED,
    LOGIN_FAILED,
    SESSION_EXPIRED,
    VALIDATION_FAILED,
    AccessError,
)
from orvex.access.oidc import group_mapping, provider_sessions
from orvex.access.storage import retry
from orvex.access.storage.instants import instant
from orvex.access.storage.store import Store, Transaction

METHOD = {"method": "password"}


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A new session: its id, for the cookie only, and whom it is for."""

    session_id: str
    user: User
    password_change_only: bool


def _failed() -> AccessError:
    return AccessError(LOGIN_FAILED, "The user name or the password is wrong.")


def _delayed(seconds: int) -> AccessError:
    return AccessError(
        LOGIN_DELAYED,
        "Too many failed sign-ins: wait, then try again.",
        {"retry_after": str(seconds)},
    )


def _expired() -> AccessError:
    return AccessError(SESSION_EXPIRED, "The session has ended: sign in again.")


@dataclass(frozen=True, slots=True)
class _Attempt:
    user: User | None
    credential: Credential | None


class SignIn:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        capacity: int = delays.CAPACITY,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock
        self._capacity = capacity

    # --- signing in and out --------------------------------------------------------------------

    def login(
        self, user_name: str, password: str, address: str | None, presented: str | None = None
    ) -> SignedIn:
        """Sign in; `presented` is the session id the browser sent, if any, which ends."""
        now = self._clock()
        found = delays.keys(user_name, address)
        attempt = self._admit(found, user_name, now)
        user, credential = attempt.user, attempt.credential
        right = (
            passwords.verifies(credential.hash, password)
            if credential is not None
            else _nobody(password)
        )
        reason = _reason(user, credential, right, now)
        if reason is not None or user is None or credential is None:
            self._record_alone("auth.login_failed", user, {**METHOD, "reason": str(reason)})
            raise _failed()
        stronger = (
            passwords.hashed(passwords.normalized(password))
            if passwords.needs_rehash(credential.hash)
            else None
        )

        def succeed(transaction: Transaction) -> SignedIn | None:
            current = directory_sql.user(transaction, user.id)
            if current is None or not current.active:
                return None  # deactivated meanwhile
            delays.succeeded(transaction, found, now)
            if group_mapping.revoke(transaction, self._audit, current.id, now):
                current = directory_sql.user(transaction, current.id) or current  # bumped
            if presented is not None:
                sessions.end(transaction, presented)
            if stronger is not None:
                credentials.replace_if_unchanged(
                    transaction, current.id, credential.hash, stronger, now
                )
            session_id = sessions.issue(
                transaction, current, now, password_change_only=credential.temporary
            )
            self._audit.record(transaction, "auth.login", current.id, METHOD)
            return SignedIn(session_id, current, credential.temporary)

        signed = self._write(succeed)
        if signed is None:
            self._record_alone("auth.login_failed", user, {**METHOD, "reason": "USER_DEACTIVATED"})
            raise _failed()
        return signed

    def logout(self, session_id: str) -> None:
        """End the session; one that is already gone needs nothing."""

        def work(transaction: Transaction) -> None:
            holder = sessions.end(transaction, session_id)
            if holder is not None:
                self._audit.record(transaction, "auth.logout", holder, {})

        self._write(work)

    def session(self, session_id: str) -> Session:
        """The live session, re-read now; `SESSION_EXPIRED` for any other."""
        now = self._clock()
        with self._store.transaction(read_only=True) as transaction:
            current = sign_in_settings.read(transaction)
            found = sessions.check(transaction, session_id, now, current, writable=False)
        if found is NEEDS_WRITE:

            def work(transaction: Transaction) -> Session | None:
                again = sessions.check(
                    transaction, session_id, now, sign_in_settings.read(transaction), writable=True
                )
                if isinstance(again, Ended):
                    self._audit.record(
                        transaction, "auth.session_expired", again.user, {"reason": again.reason}
                    )
                return again if isinstance(again, Session) else None

            found = self._write(work)
        if isinstance(found, Session):
            return found
        raise _expired()

    # --- passwords -----------------------------------------------------------------------------

    def change_password(self, session_id: str, current: str, new: str, address: str | None) -> str:
        """Change the session user's password; return the session's new id."""
        user = self.session(session_id).user
        chosen = passwords.checked(new, user.user_name)
        now = self._clock()
        found = delays.keys(user.user_name, address)
        credential = self._admit(found, user.user_name, now).credential
        right = (
            passwords.verifies(credential.hash, current)
            if credential is not None
            else _nobody(current)
        )
        reason = _reason(user, credential, right, now)
        if reason is not None or credential is None:
            self._record_alone("auth.login_failed", user, {**METHOD, "reason": str(reason)})
            raise _failed()
        stronger = passwords.hashed(chosen)

        def work(transaction: Transaction) -> str:
            live = sessions.check(
                transaction, session_id, now, sign_in_settings.read(transaction), writable=True
            )
            if not isinstance(live, Session) or live.user.id != user.id:
                raise _expired()
            if not credentials.replace_if_unchanged(
                transaction, user.id, credential.hash, stronger, now, permanent=True
            ):
                raise _failed()  # an administrator reset it meanwhile
            delays.succeeded(transaction, found, now)
            provider = provider_sessions.of(transaction, sessions.session_hash(session_id))
            sessions.end_all(transaction, user.id)
            renewed = sessions.issue(transaction, live.user, now, password_change_only=False)
            if provider is not None:  # still a provider session, refreshed on the same clock
                provider_sessions.record(transaction, renewed, *provider)
            self._audit.record(transaction, "user.password_changed", user.id, {"user": user.id})
            return renewed

        return self._write(work)

    def reset_password(self, actor: str, target: str) -> str:
        """An administrator's reset: a temporary password, returned once; every session ends."""
        temporary = passwords.temporary()
        stored = passwords.hashed(temporary)
        now = self._clock()

        def work(transaction: Transaction) -> None:
            refused(rules.manage_users(directory_sql.caller(transaction, actor), target))
            if directory_sql.user(transaction, target) is None:
                raise AccessError(VALIDATION_FAILED, "The user is not valid.", {"field": "user"})
            credentials.store(transaction, target, stored, now, temporary=True)
            sessions.end_all(transaction, target)
            self._audit.record(transaction, "user.password_reset", actor, {"user": target})

        audited(self._store, self._audit, "reset_password", actor, work, target=target)
        return temporary

    # --- shared steps --------------------------------------------------------------------------

    def _admit(self, found: delays.Keys, user_name: str, now: datetime) -> _Attempt:
        """Reserve the attempt; `LOGIN_DELAYED` while a delay is active."""

        def work(transaction: Transaction) -> tuple[int, _Attempt]:
            wait = delays.admit(transaction, found, now, self._capacity)
            canonical = canonical_user_name(user_name)
            user = (
                directory_sql.user_by_name(transaction, canonical)
                if canonical is not None
                else None
            )
            if wait:
                self._audit.record(
                    transaction,
                    "auth.login_failed",
                    user.id if user is not None else SYSTEM,
                    {**METHOD, "reason": LOGIN_DELAYED},
                )
                return wait, _Attempt(None, None)
            held = credentials.of(transaction, user.id) if user is not None else None
            return 0, _Attempt(user, held)

        wait, attempt = self._write(work)
        if wait:
            raise _delayed(wait)
        return attempt

    def _record_alone(self, kind: str, user: User | None, details: dict[str, str]) -> None:
        def work(transaction: Transaction) -> None:
            self._audit.record(transaction, kind, user.id if user is not None else SYSTEM, details)

        self._write(work)

    def _write[T](self, work: Callable[[Transaction], T]) -> T:
        try:
            done = retry.write(self._store, work)
        except AccessError as error:
            if error.code.startswith("AUDIT_"):
                self._audit.unavailable()
            raise
        self._audit.committed()
        return done


def _nobody(password: str) -> bool:
    passwords.verify_nobody(password)
    return False


def _reason(
    user: User | None, credential: Credential | None, right: bool, now: datetime
) -> str | None:
    """Why a password check fails, for the audit log only; None when it succeeds."""
    if user is None:
        return "UNKNOWN_USER"
    if credential is None:
        return "NO_PASSWORD"
    if not right:
        return "WRONG_PASSWORD"
    if not user.active:
        return "USER_DEACTIVATED"
    if credential.expired(instant(now)):
        return "PASSWORD_EXPIRED"
    return None
