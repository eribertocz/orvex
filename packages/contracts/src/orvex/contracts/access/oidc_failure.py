"""The one refusal of an OIDC step (#439), shared by `access` and the provider fetcher the server
composes it with: `OIDC_FAILED` for the browser, with a reason from a closed list for the audit
log alone."""

from __future__ import annotations

from orvex.contracts.access.errors import OIDC_FAILED, AccessError

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


def failed(reason: str) -> AccessError:
    if reason not in REASONS:
        raise ValueError(f"not an OIDC failure reason: {reason}")
    return AccessError(OIDC_FAILED, "Sign-in failed.", {"reason": reason})
