"""What a sign-in's start leaves for its callback (application, #439): `state`, `nonce` and the
PKCE verifier, kept server-side for at most 10 minutes and used once.

The browser holds only an opaque reference in a cookie; only its SHA-256 is stored. A callback
takes the binding (deleted whatever the outcome) and must present the same `state`, compared in
constant time. A binding made to refresh a session's groups names that session, so its outcome
can end it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from orvex.access.oidc.failure import failed
from orvex.access.storage.instants import instant, parsed
from orvex.access.storage.store import Transaction

LIFETIME = timedelta(minutes=10)
_BYTES = 32  # 256 bits each


@dataclass(frozen=True, slots=True)
class Binding:
    provider: str
    state: str
    nonce: str
    verifier: str
    refreshing: str | None

    @property
    def challenge(self) -> str:
        """The PKCE S256 challenge (`plain` is never sent)."""
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _random() -> str:
    return secrets.token_urlsafe(_BYTES)


def _hash(reference: str) -> str:
    return hashlib.sha256(reference.encode()).hexdigest()


def create(
    transaction: Transaction, provider: str, now: datetime, refreshing: str | None = None
) -> tuple[str, Binding]:
    """A new binding; returns the reference for the cookie, which is stored nowhere."""
    transaction.run(
        "DELETE FROM oidc_bindings WHERE created_at < :old", old=instant(now - LIFETIME)
    )
    reference = _random()
    binding = Binding(provider, _random(), _random(), _random(), refreshing)
    transaction.run(
        "INSERT INTO oidc_bindings (reference, provider, state, nonce, verifier, created_at, "
        "refreshing) VALUES (:reference, :provider, :state, :nonce, :verifier, :now, :refreshing)",
        reference=_hash(reference),
        provider=provider,
        state=binding.state,
        nonce=binding.nonce,
        verifier=binding.verifier,
        now=instant(now),
        refreshing=refreshing,
    )
    return reference, binding


@dataclass(frozen=True, slots=True)
class Taken:
    """A binding as it was deleted, not yet checked."""

    binding: Binding
    created_at: datetime


def take(transaction: Transaction, reference: str | None) -> Taken | None:
    """Delete the binding `reference` names and return it. The caller commits before checking
    it (`checked`), so a failed callback can never leave the binding usable."""
    if not reference or len(reference) > 64:
        return None
    found = transaction.rows(
        "DELETE FROM oidc_bindings WHERE reference = :reference "
        "RETURNING provider, state, nonce, verifier, created_at, refreshing",
        reference=_hash(reference),
    )
    if not found:
        return None
    provider, state, nonce, verifier, created, refreshing = found[0]
    binding = Binding(
        str(provider),
        str(state),
        str(nonce),
        str(verifier),
        None if refreshing is None else str(refreshing),
    )
    return Taken(binding, parsed(str(created)))


def checked(taken: Taken | None, provider: str, state: str | None, now: datetime) -> Binding:
    """`OIDC_FAILED` (`state`) unless the binding existed, is under 10 minutes old, is for this
    provider, and `state` matches it in constant time."""
    if taken is None or taken.binding.provider != provider or now >= taken.created_at + LIFETIME:
        raise failed("state")
    expected = taken.binding.state.encode()
    if not isinstance(state, str) or not hmac.compare_digest(state.encode(), expected):
        raise failed("state")
    return taken.binding
