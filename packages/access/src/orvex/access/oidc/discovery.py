"""A provider's discovery document and keys (application, #439): read through the server's
fetcher, checked, and the keys cached.

- The discovery document's `issuer` must equal the configured one exactly.
- Its endpoints are `https`. The token and key endpoints must be on the issuer's host or on
  a host the administrator lists for the provider (`endpoint_hosts`).
- If the document lists PKCE methods, `S256` must be among them.
- Keys are cached per provider. A token naming an unknown key reads them again at most once a
  minute, so a flood of forged tokens is never a flood of fetches.

`access` opens no connection itself: the `Fetcher` (the server's `provider_fetch.py`) does, under
its SSRF rules, and every failure is `OIDC_FAILED` with the reason `network`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from orvex.access.errors import OIDC_FAILED, OIDC_NOT_CONFIGURED, AccessError
from orvex.access.oidc.configuration import Provider

KEYS_REFRESH = timedelta(minutes=1)


class Fetcher(Protocol):
    """The server's requests to a provider, under its SSRF rules."""

    def get(self, provider: Provider, url: str) -> bytes: ...

    def post_form(
        self, provider: Provider, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes: ...


def failed(reason: str) -> AccessError:
    """The one refusal a browser sees; `reason` is for the audit log alone."""
    return AccessError(OIDC_FAILED, "Sign-in failed.", {"reason": reason})


@dataclass(frozen=True, slots=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    iss_parameter: bool


def _https(url: object) -> str | None:
    if not isinstance(url, str) or len(url) > 2048 or not url.isascii():
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return (parts.hostname or "").lower() if parts.scheme == "https" and parts.hostname else None


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def checked_metadata(provider: Provider, document: bytes, *, at_start: bool) -> Metadata:
    """The discovery document, checked; refused as `OIDC_NOT_CONFIGURED` at start, as
    `OIDC_FAILED` (`issuer` or `network`) afterwards."""

    def refused(setting: str, reason: str) -> AccessError:
        if at_start:
            return AccessError(
                OIDC_NOT_CONFIGURED,
                f"The sign-in setting {setting} is not valid.",
                {"setting": f"provider.{provider.name}.{setting}"},
            )
        return failed(reason)

    try:
        found = json.loads(document)
    except (ValueError, UnicodeDecodeError) as error:
        raise refused("issuer", "network") from error
    if not isinstance(found, dict):
        raise refused("issuer", "network")
    if found.get("issuer") != provider.issuer:
        raise refused("issuer", "issuer")
    allowed = {provider.issuer_host, *provider.endpoint_hosts}
    authorization = found.get("authorization_endpoint")
    if _https(authorization) is None:
        raise refused("issuer", "network")
    for key in ("token_endpoint", "jwks_uri"):
        if _https(found.get(key)) not in allowed:
            raise refused("endpoint_hosts", "network")
    methods = found.get("code_challenge_methods_supported")
    if methods is not None and (not isinstance(methods, list) or "S256" not in methods):
        raise refused("issuer", "network")
    return Metadata(
        issuer=provider.issuer,
        authorization_endpoint=str(authorization),
        token_endpoint=str(found["token_endpoint"]),
        jwks_uri=str(found["jwks_uri"]),
        iss_parameter=found.get("authorization_response_iss_parameter_supported") is True,
    )


class Keys:
    """A provider's signing keys, read again at most once a minute for an unknown key."""

    def __init__(
        self,
        provider: Provider,
        metadata: Metadata,
        fetcher: Fetcher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._metadata = metadata
        self._fetcher = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: KeySet | None = None
        self._read_at: datetime | None = None
        self.reads = 0

    def current(self, kid: str | None) -> KeySet:
        """The keys, read again first when `kid` is unknown and the last read is a minute old."""
        with self._lock:
            known = self._keys is not None and (
                kid is None or any(key.kid == kid for key in self._keys)
            )
            now = self._clock()
            due = self._read_at is None or now - self._read_at >= KEYS_REFRESH
            if not known and due:
                self._read_at = now
                self.reads += 1
                self._keys = self._read()
            if self._keys is None:  # the last read failed less than a minute ago
                raise failed("network")
            return self._keys

    def _read(self) -> KeySet:
        document = self._fetcher.get(self._provider, self._metadata.jwks_uri)
        try:
            found = json.loads(document)
            if not isinstance(found, dict) or not isinstance(found.get("keys"), list):
                raise ValueError("not a key set")
            usable = [
                key
                for key in found["keys"]
                if isinstance(key, dict) and key.get("use", "sig") == "sig" and "d" not in key
            ]
            return KeySet.import_key_set({"keys": usable})
        except (ValueError, UnicodeDecodeError, JoseError, TypeError) as error:
            raise failed("network") from error
