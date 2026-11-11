"""The server's own requests to an OIDC provider (#439), under SSRF rules: the `Fetcher` that
`orvex.access.oidc` reads discovery documents, keys and tokens through.

- Only `https`, with the certificate and host name verified. No redirect is followed, and no
  environment proxy is used.
- Each request has a 5-second deadline for the whole exchange (a watchdog cuts a slow one, even
  in its headers) and reads at most 1 MiB.
- The host must be the issuer's, or one the administrator lists for the provider
  (`endpoint_hosts`).
- **Addresses.** The host name is resolved once, and every address it gives is checked. The
  connection goes to a checked address, with SNI and `Host` kept, so a DNS answer that changes
  between the check and the connection (DNS rebinding) cannot redirect it.
- **Refused:** any address that is not globally routable. That covers loopback, link-local
  (cloud metadata at 169.254.169.254 included), unspecified, multicast, private (RFC 1918,
  ULA), carrier-grade NAT and reserved ranges. It applies to IPv4, IPv6 and IPv4-mapped IPv6
  alike.
- **Authorized internal providers.** A provider marked `internal` may also reach addresses inside
  the networks listed for it. Loopback, link-local, unspecified and multicast stay refused even
  then.

Every refusal is `OIDC_FAILED` with the reason `network`, or `token_exchange` for an error from
the token endpoint. Its message never holds a URL's query, a form or a secret.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit

from orvex.access.oidc.configuration import Provider
from orvex.access.oidc.discovery import failed

TIMEOUT = 5.0
LIMIT = 1024 * 1024
_CHUNK = 64 * 1024

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int], list[str]]
type Connector = Callable[[str, int, float], socket.socket]


def _resolve(host: str, port: int) -> list[str]:
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(item[4][0]) for item in found))


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


def allowed(address: Address, provider: Provider) -> bool:
    """Whether a provider's request may reach `address`."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        return False
    if address.is_global:
        return True
    return provider.internal and any(
        address.version == network.version and address in network
        for network in provider.internal_ranges
    )


def _abort(connection: http.client.HTTPSConnection) -> None:
    """Cut a request past its deadline: a blocked read then fails at once."""
    sock = connection.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection to one checked address, verifying the certificate for `host`."""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        context: ssl.SSLContext,
        connector: Connector,
        timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._connector = connector
        self._tls = context

    def connect(self) -> None:
        raw = self._connector(self._address, self.port, float(self.timeout or TIMEOUT))
        try:
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class ProviderFetch:
    def __init__(
        self,
        *,
        context: ssl.SSLContext | None = None,
        resolver: Resolver = _resolve,
        connector: Connector = _connect,
        timeout: float = TIMEOUT,
        limit: int = LIMIT,
    ) -> None:
        self._context = context or ssl.create_default_context()
        if self._context.verify_mode != ssl.CERT_REQUIRED or not self._context.check_hostname:
            raise ValueError("the provider TLS context must verify the certificate and host")
        self._resolver = resolver
        self._connector = connector
        self._timeout = timeout
        self._limit = limit

    def get(self, provider: Provider, url: str) -> bytes:
        return self._request(provider, "GET", url, None, {})

    def post_form(
        self, provider: Provider, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes:
        user, secret = (quote(part, safe="") for part in basic)  # RFC 6749 §2.3.1
        credentials = base64.b64encode(f"{user}:{secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._request(provider, "POST", url, urlencode(form).encode(), headers)

    def _request(
        self,
        provider: Provider,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> bytes:
        try:
            parts = urlsplit(url)
            port = parts.port or 443
        except ValueError as error:
            raise failed("network") from error
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or host not in {provider.issuer_host, *provider.endpoint_hosts}
        ):
            raise failed("network")
        deadline = time.monotonic() + self._timeout
        address = self._checked_address(provider, host, port)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection = _Pinned(host, port, address, self._context, self._connector, self._timeout)
        watchdog = threading.Timer(self._timeout, _abort, [connection])  # a whole-request deadline
        watchdog.daemon = True
        watchdog.start()
        answer: http.client.HTTPResponse | None = None
        try:
            connection.request(
                method, path, body=body, headers={**headers, "Accept": "application/json"}
            )
            answer = connection.getresponse()
            content = self._read(answer, deadline)
            status = answer.status
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise failed("network") from error
        finally:
            watchdog.cancel()
            if answer is not None:
                answer.close()  # it holds the socket's file: close it, then the socket
            connection.close()
        if status != 200:
            raise failed("token_exchange" if method == "POST" and status < 500 else "network")
        return content

    def _checked_address(self, provider: Provider, host: str, port: int) -> str:
        try:
            found = [ipaddress.ip_address(item) for item in self._resolver(host, port)]
        except (OSError, ValueError) as error:
            raise failed("network") from error
        if not found or not all(allowed(address, provider) for address in found):
            raise failed("network")
        return str(found[0])

    def _read(self, answer: http.client.HTTPResponse, deadline: float) -> bytes:
        kept = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise failed("network")
            chunk = answer.read1(_CHUNK) if hasattr(answer, "read1") else answer.read(_CHUNK)
            if not chunk:
                return bytes(kept)
            kept.extend(chunk)
            if len(kept) > self._limit:
                raise failed("network")
