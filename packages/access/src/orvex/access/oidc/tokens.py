"""ID-token validation (domain, #439): what an OIDC provider says about who signed in, believed
only after every check.

- **Signature.** The algorithm must be one the configuration allows. The token's header is never
  trusted to pick it, so `none` and `HS*` (the client secret as an HMAC key) can never pass. The
  key is the one whose `kid` the header names, of the key type that algorithm needs; without
  `kid`, the provider's only key of that type.
- **Claims.**
  - `iss` must equal the issuer exactly.
  - `aud` must contain `client_id`; with several audiences `azp` must be present, and an `azp`
    that is present must equal `client_id`.
  - `exp`, `iat` and `nbf` are checked with at most 60 seconds of clock skew.
  - `nonce` must equal the binding's, compared in constant time.
  - `sub` must be a non-empty string.
- Each failure is `OIDC_FAILED`, with the reason `signature`, `issuer`, `claims` or `nonce`.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from orvex.access.oidc.configuration import Provider
from orvex.access.oidc.discovery import Keys
from orvex.access.oidc.failure import failed

SKEW = timedelta(seconds=60)
_LONGEST_TOKEN = 64 * 1024
_KEY_TYPE = {"RS256": "RSA", "PS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}


@dataclass(frozen=True, slots=True)
class Identity:
    issuer: str
    subject: str
    claims: Mapping[str, object]


def _header(token: str) -> Mapping[str, object]:
    if len(token) > _LONGEST_TOKEN or not token.isascii():
        raise failed("signature")
    try:
        return jws.extract_compact(token.encode()).headers()
    except (JoseError, ValueError, TypeError) as error:
        raise failed("signature") from error


def _key(keys: KeySet, algorithm: str, kid: object) -> object:
    wanted = _KEY_TYPE[algorithm]
    candidates = [key for key in keys if key.key_type == wanted and (kid is None or key.kid == kid)]
    if len(candidates) != 1:
        raise failed("signature")
    return candidates[0]


def _instant(claims: Mapping[str, object], name: str) -> datetime | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise failed("claims")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise failed("claims") from error


def validated(provider: Provider, keys: Keys, token: str, nonce: str, now: datetime) -> Identity:
    header = _header(token)
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in provider.algorithms:
        raise failed("signature")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise failed("signature")
    key = _key(keys.current(kid), algorithm, kid)
    try:
        decoded = jwt.decode(token, key, algorithms=[algorithm])  # type: ignore[arg-type]
    except (JoseError, ValueError, TypeError) as error:
        raise failed("signature") from error
    claims = decoded.claims
    if claims.get("iss") != provider.issuer:
        raise failed("issuer")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or provider.client_id not in audiences:
        raise failed("claims")
    party = claims.get("azp")
    if (len(audiences) > 1 and party is None) or (
        party is not None and party != provider.client_id
    ):
        raise failed("claims")
    expires, issued, before = (_instant(claims, name) for name in ("exp", "iat", "nbf"))
    if expires is None or issued is None:
        raise failed("claims")
    if now >= expires + SKEW or issued > now + SKEW or (before is not None and before > now + SKEW):
        raise failed("claims")
    given = claims.get("nonce")
    if not isinstance(given, str) or not hmac.compare_digest(given.encode(), nonce.encode()):
        raise failed("nonce")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        raise failed("claims")
    return Identity(provider.issuer, subject, claims)
