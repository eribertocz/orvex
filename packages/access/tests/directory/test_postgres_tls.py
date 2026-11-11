"""#436 (Owner, 2026-11-10): a remote PostgreSQL is reached only with TLS that checks the
certificate and the host name, against a real server with TLS.

The tests reuse #120's server: `STORE_TEST_POSTGRES_TLS` (reached through 127.0.0.2, a host that
is not this machine for the connection rules), `STORE_TEST_POSTGRES_TLS_CA` (its certificate
authority) and `STORE_TEST_POSTGRES_PASSWORD`. CI provides all three; elsewhere the tests are
skipped when they are missing. The server's certificate names `localhost`, 127.0.0.1 and
127.0.0.2, never 127.0.0.3.
"""

from __future__ import annotations

import os
import ssl
import uuid
from collections.abc import Iterator

import pg8000.native
import pytest
from orvex.access.directory import PostgresStore, migrate, read_settings

TLS = "STORE_TEST_POSTGRES_TLS"
AUTHORITY = "STORE_TEST_POSTGRES_TLS_CA"
PASSWORD = "STORE_TEST_POSTGRES_PASSWORD"


@pytest.fixture
def server() -> Iterator[tuple[str, str, str, str]]:
    connection, authority = os.environ.get(TLS, ""), os.environ.get(AUTHORITY, "")
    password = os.environ.get(PASSWORD, "")
    if not connection or not authority or not password:
        if os.environ.get("CI"):
            pytest.fail(f"{TLS}, {AUTHORITY} and {PASSWORD} are required in CI")
        pytest.skip("no PostgreSQL with TLS configured for this run")
    schema = f"t_{uuid.uuid4().hex[:16]}"
    yield connection, authority, password, schema
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    # Cleanup only: TLS without checks is acceptable for dropping a test's schema.
    admin = pg8000.native.Connection(
        keywords["user"],
        password=password,
        host=keywords["host"],
        port=int(keywords["port"]),
        database=keywords["dbname"],
    )
    try:
        admin.run(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        admin.close()


def test_verify_full_with_the_authority_connects_over_tls(
    server: tuple[str, str, str, str],
) -> None:
    connection, authority, password, schema = server
    settings = read_settings(
        f"{connection} sslmode=verify-full sslrootcert={authority}", password, schema=schema
    )
    store = PostgresStore(settings)
    try:
        assert migrate(store) == 1
        with store.transaction(read_only=True) as transaction:
            rows = transaction.rows("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        assert rows == [(True,)]
    finally:
        store.close()


def test_a_certificate_from_an_unknown_authority_is_refused(
    server: tuple[str, str, str, str],
) -> None:
    connection, _, password, schema = server
    store = PostgresStore(
        read_settings(f"{connection} sslmode=verify-full", password, schema=schema)
    )
    with pytest.raises(ssl.SSLCertVerificationError, match="unable to get local issuer"):
        migrate(store)
    store.close()


def test_a_certificate_for_another_host_name_is_refused(
    server: tuple[str, str, str, str],
) -> None:
    connection, authority, password, schema = server
    elsewhere = connection.replace("host=127.0.0.2", "host=127.0.0.3")
    store = PostgresStore(
        read_settings(
            f"{elsewhere} sslmode=verify-full sslrootcert={authority}", password, schema=schema
        )
    )
    with pytest.raises(ssl.SSLCertVerificationError, match="IP address mismatch"):
        migrate(store)
    store.close()
