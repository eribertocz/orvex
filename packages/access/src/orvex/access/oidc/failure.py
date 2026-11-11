"""The one refusal of an OIDC step (domain, #439): `OIDC_FAILED` for the browser, with a reason
from a closed list for the audit log alone."""

from __future__ import annotations

from orvex.access.errors import OIDC_FAILED, AccessError

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
