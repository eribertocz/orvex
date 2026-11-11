"""#439 AC11, R5: the server's requests to a provider, under SSRF rules. A real TLS server on
loopback stands in for the provider; the resolver and the connector are replaced so the address
checks see the addresses each case needs."""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from orvex.access.errors import OIDC_FAILED, AccessError
from orvex.access.oidc.configuration import Provider, parsed
from orvex.server.provider_fetch import ProviderFetch, allowed

HOST = "idp.test"
PUBLIC = "93.184.215.14"


def provider(**settings: object) -> Provider:
    document = {
        "providers": {
            "corp": {
                "issuer": f"https://{HOST}/realms/corp",
                "client_id": "bi-client",
                "client_secret_variable": "SECRET",
                "algorithms": ["RS256"],
                **settings,
            }
        }
    }
    return parsed(document, {"SECRET": "s3cret"}).provider("corp")


def _certificates(directory: Path) -> tuple[Path, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    now = datetime.datetime.now(datetime.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    paths = directory / "ca.pem", directory / "leaf.pem", directory / "leaf.key"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"https://{HOST}/ok")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * (1024 * 1024 + 10))
        elif self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass  # the client cut it off, as it should
        elif self.path == "/slow-headers":
            time.sleep(2)
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass  # the client cut it off, as it should
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.send_response(400 if b"bad" in body else 200)
        self.end_headers()
        self.wfile.write(self.headers.get("Authorization", "").encode())


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[int, ssl.SSLContext]]:
    ca, leaf, key = _certificates(tmp_path)
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(leaf, key)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = serving.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = ssl.create_default_context(cafile=str(ca))
    yield httpd.server_address[1], client
    httpd.shutdown()
    httpd.server_close()


def fetch(
    server: tuple[int, ssl.SSLContext],
    addresses: list[str],
    seen: list[str] | None = None,
    timeout: float = 5.0,
) -> ProviderFetch:
    port, context = server

    def connect(address: str, _port: int, timeout: float) -> socket.socket:
        if seen is not None:
            seen.append(address)
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    return ProviderFetch(
        context=context,
        resolver=lambda host, port: list(addresses),
        connector=connect,
        timeout=timeout,
    )


def network(action: object) -> None:
    with pytest.raises(AccessError) as caught:
        action()  # type: ignore[operator]
    assert caught.value.code == OIDC_FAILED
    assert caught.value.details["reason"] == "network"


def test_a_public_address_is_reached_with_the_certificate_and_host_checked(
    server: tuple[int, ssl.SSLContext],
) -> None:
    seen: list[str] = []
    assert fetch(server, [PUBLIC], seen).get(provider(), f"https://{HOST}/ok") == b'{"ok": true}'
    assert seen == [PUBLIC]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.1",
        "::ffff:10.0.0.1",
        "fd00::1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_each_refused_address_class(server: tuple[int, ssl.SSLContext], address: str) -> None:
    seen: list[str] = []
    network(lambda: fetch(server, [address], seen).get(provider(), f"https://{HOST}/ok"))
    assert seen == []  # never connected


def test_one_refused_address_among_the_answers_refuses_the_host(
    server: tuple[int, ssl.SSLContext],
) -> None:
    network(lambda: fetch(server, [PUBLIC, "10.0.0.1"]).get(provider(), f"https://{HOST}/ok"))


def test_a_redirect_is_not_followed(server: tuple[int, ssl.SSLContext]) -> None:
    network(lambda: fetch(server, [PUBLIC]).get(provider(), f"https://{HOST}/redirect"))


def test_another_host_is_refused_unless_listed(server: tuple[int, ssl.SSLContext]) -> None:
    network(lambda: fetch(server, [PUBLIC]).get(provider(), "https://elsewhere.test/ok"))
    network(lambda: fetch(server, [PUBLIC]).get(provider(), f"http://{HOST}/ok"))
    listed = provider(endpoint_hosts=["elsewhere.test"])
    with pytest.raises(AccessError):  # listed, so tried: the certificate is for idp.test only
        fetch(server, [PUBLIC]).get(listed, "https://elsewhere.test/ok")


def test_dns_rebinding_reaches_only_the_checked_address(server: tuple[int, ssl.SSLContext]) -> None:
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    seen: list[str] = []
    port, context = server

    def connect(address: str, _port: int, timeout: float) -> socket.socket:
        seen.append(address)
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)

    client = ProviderFetch(
        context=context, resolver=lambda host, p: next(answers), connector=connect
    )
    client.get(provider(), f"https://{HOST}/ok")
    assert seen == [PUBLIC]  # resolved once, connected to what was checked


def test_an_oversized_response_is_cut_off(server: tuple[int, ssl.SSLContext]) -> None:
    network(lambda: fetch(server, [PUBLIC]).get(provider(), f"https://{HOST}/big"))


def test_a_slow_response_is_cut_off_at_the_deadline(server: tuple[int, ssl.SSLContext]) -> None:
    started = time.monotonic()
    network(lambda: fetch(server, [PUBLIC], timeout=1.0).get(provider(), f"https://{HOST}/slow"))
    network(
        lambda: fetch(server, [PUBLIC], timeout=1.0).get(provider(), f"https://{HOST}/slow-headers")
    )
    assert time.monotonic() - started < 4


def test_a_certificate_for_another_host_is_refused(server: tuple[int, ssl.SSLContext]) -> None:
    other = provider(issuer="https://other.test/realms/corp")
    network(lambda: fetch(server, [PUBLIC]).get(other, "https://other.test/ok"))


def test_an_unverified_context_is_refused() -> None:
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verify"):
        ProviderFetch(context=insecure)


def test_the_token_endpoint_gets_basic_authentication_and_its_errors(
    server: tuple[int, ssl.SSLContext],
) -> None:
    client = fetch(server, [PUBLIC])
    answer = client.post_form(
        provider(), f"https://{HOST}/token", {"code": "c"}, ("bi:client", "s3 cret")
    )
    assert answer == b"Basic YmklM0FjbGllbnQ6czMlMjBjcmV0"  # each part form-encoded first
    with pytest.raises(AccessError) as caught:
        client.post_form(provider(), f"https://{HOST}/token", {"code": "bad"}, ("bi-client", "s"))
    assert caught.value.details["reason"] == "token_exchange"


def test_an_internal_provider_reaches_its_range_and_nothing_else(
    server: tuple[int, ssl.SSLContext],
) -> None:
    internal = provider(internal=True, internal_ranges=["10.20.0.0/16"])
    assert fetch(server, ["10.20.1.2"]).get(internal, f"https://{HOST}/ok") == b'{"ok": true}'
    network(lambda: fetch(server, ["10.21.0.1"]).get(internal, f"https://{HOST}/ok"))
    network(lambda: fetch(server, ["127.0.0.1"]).get(internal, f"https://{HOST}/ok"))
    assert not allowed(ipaddress.ip_address("169.254.169.254"), internal)
    for address in ("::1", "::", "::7f00:1", "::ffff:127.0.0.1", "fe80::1"):
        assert not allowed(ipaddress.ip_address(address), internal)
