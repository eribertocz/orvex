"""#436: the PostgreSQL store's connection rules, checked before anything connects: only the
known keywords, a password, and TLS with the certificate and host name checked for any server
that is not this machine."""

from __future__ import annotations

import ssl

import pytest
from orvex.access.directory import PostgresStore, read_settings
from orvex.access.errors import VALIDATION_FAILED, AccessError

BASE = "port=5432 dbname=orvex user=access"


@pytest.mark.parametrize(
    "connection",
    [
        f"host=localhost {BASE}",
        f"host=127.0.0.1 {BASE} sslmode=disable",
        f"host=::1 {BASE}",
        f"host=/var/run/postgresql {BASE}",
        f"host=db.example.com {BASE} sslmode=verify-full",
        f"host=db.example.com {BASE} sslmode=verify-full sslrootcert=/etc/ssl/ca.pem",
    ],
)
def test_accepted_connections(connection: str) -> None:
    assert read_settings(connection, "secret").keywords["dbname"] == "orvex"


@pytest.mark.parametrize(
    "connection",
    [
        f"host=db.example.com {BASE}",  # another machine without TLS
        f"host=db.example.com {BASE} sslmode=disable",
        f"host=db.example.com {BASE} sslmode=require",  # TLS without checking the certificate
        f"host=localhost {BASE} sslmode=prefer",
        f"host=localhost {BASE} password=inline",  # the password never travels in the string
        f"host=localhost {BASE} service=other",  # a service file can name any host
        "port=5432 dbname=orvex user=access",  # no host
        f"host=localhost {BASE} options=-csearch_path=public",
        f"host=localhost {BASE} broken",
    ],
)
def test_refused_connections(connection: str) -> None:
    with pytest.raises(AccessError) as refused:
        read_settings(connection, "secret")
    assert refused.value.code == VALIDATION_FAILED


def test_a_password_is_required() -> None:
    with pytest.raises(AccessError):
        read_settings(f"host=localhost {BASE}", "")


@pytest.mark.parametrize("schema", ["Access", "access; drop", "1access", "a" * 64, ""])
def test_the_schema_is_a_plain_lower_case_identifier(schema: str) -> None:
    with pytest.raises(AccessError):
        read_settings(f"host=localhost {BASE}", "secret", schema=schema)


def test_verify_full_checks_the_certificate_and_the_host_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Refused(Exception):
        pass

    def connect(user: str, **options: object) -> None:
        seen.update(options)
        raise Refused

    monkeypatch.setattr("pg8000.native.Connection", connect)
    store = PostgresStore(read_settings(f"host=db.example.com {BASE} sslmode=verify-full", "s"))
    with pytest.raises(Refused), store.transaction():
        pass
    context = seen["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_without_tls_the_driver_is_told_no_tls_never_unchecked_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Refused(Exception):
        pass

    def connect(user: str, **options: object) -> None:
        seen.update(options)
        raise Refused

    monkeypatch.setattr("pg8000.native.Connection", connect)
    store = PostgresStore(read_settings(f"host=localhost {BASE}", "s"))
    with pytest.raises(Refused), store.transaction():
        pass
    assert seen["ssl_context"] is False  # pg8000 reads None as TLS without verification
