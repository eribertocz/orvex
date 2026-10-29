"""#120: where the lake's catalog lives — the TLS rule for PostgreSQL, a SQLite catalog and a
DuckDB catalog outside the lake folder.

The rule tests run anywhere (the refusal comes before anything is opened). The TLS service
tests need `STORE_TEST_POSTGRES_TLS` (a libpq connection to a PostgreSQL with TLS, reached by a
host other than the local machine) and `STORE_TEST_POSTGRES_TLS_CA` (its certificate
authority's file); CI provides both.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, _conninfo, _require_tls, _with_root_cert, open_lake

LIMITS = Limits(
    memory_limit="512MB", threads=1, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"


def postgres(connection: str, tmp_path: Path) -> PostgresLakeLocation:
    return PostgresLakeLocation(
        kind="lake_location",
        catalog="postgres",
        connection=connection,
        password_env=PASSWORD_ENV,
        metadata_schema=f"t_{uuid.uuid4().hex[:16]}",
        data_path=(tmp_path / "data").as_posix() + "/",
    )


def write_and_read(lake: Lake) -> list[dict[str, object]]:
    with lake.transaction() as transaction:
        transaction.execute(
            StatementRequest(
                kind="statement",
                sql="CREATE TABLE t AS SELECT 42::DECIMAL(18,2) AS amount",
                parameters=[],
            )
        )
    request = ReadRequest(
        kind="read",
        sql="SELECT CAST(amount AS VARCHAR) AS amount FROM t",
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=16,
    )
    found: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return found


# --- libpq connection strings ----------------------------------------------------------------


def test_connection_strings_are_read_as_libpq_reads_them() -> None:
    assert _conninfo("host=db port = 5432  dbname='my lake' user=a\\ b") == {
        "host": "db",
        "port": "5432",
        "dbname": "my lake",
        "user": "a b",
    }
    assert _conninfo("sslrootcert='/c/it\\'s ca.crt' HOST=x host=y") == {
        "sslrootcert": "/c/it's ca.crt",
        "host": "y",  # keywords are case-insensitive here; the last one wins
    }
    for broken in ["host", "host=db 'x'", "dbname='open", "=db"]:
        with pytest.raises(StoreError) as refused:
            _conninfo(broken)
        assert refused.value.code == "STORE_STATEMENT_INVALID"


# --- the TLS rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "connection",
    [
        "host=db.example.com dbname=lake user=u",
        *(
            f"host=db.example.com dbname=lake user=u sslmode={mode}"
            for mode in ["disable", "allow", "prefer", "require", "verify-ca"]
        ),
        "host=127.0.0.2 dbname=lake user=u sslmode=require",
        "host=localhost,db.example.com dbname=lake user=u",  # one host is another machine
        "host=localhost hostaddr=10.0.0.5 dbname=lake user=u",  # the address that is used
        "host=localhost service=lake dbname=lake user=u",  # a service can name any host
        "host='db.example.com sslmode=verify-full' dbname=lake",  # a quoted value, not a pair
    ],
)
def test_a_remote_catalog_without_verify_full_is_refused(
    connection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "not-used")
    with pytest.raises(StoreError) as refused:
        open_lake(postgres(connection, tmp_path), LIMITS)
    assert refused.value.code == "STORE_TLS_REQUIRED"
    assert "not-used" not in str(refused.value.details)
    # Nothing was created for a refused location.
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "connection",
    [
        "host=localhost dbname=lake user=u",
        "host=127.0.0.1 dbname=lake user=u",
        "host=::1 dbname=lake user=u",
        "host=/var/run/postgresql dbname=lake user=u",  # a Unix-socket directory
        "dbname=lake user=u",  # libpq's default socket
        "host=localhost hostaddr=127.0.0.1 dbname=lake user=u",
        "host=db.example.com dbname=lake user=u sslmode=verify-full",
    ],
)
def test_local_catalogs_and_verify_full_are_accepted(
    connection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    _require_tls(connection)


def test_the_environment_counts_when_the_connection_names_no_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", "db.example.com")
    with pytest.raises(StoreError) as refused:
        _require_tls("dbname=lake user=u")
    assert refused.value.code == "STORE_TLS_REQUIRED"
    _require_tls("host=localhost dbname=lake user=u")  # a named host overrides PGHOST
    monkeypatch.delenv("PGHOST")
    monkeypatch.setenv("PGSERVICE", "lake")
    with pytest.raises(StoreError):
        _require_tls("host=localhost dbname=lake user=u")


def test_verified_tls_uses_the_system_authorities_unless_told_otherwise() -> None:
    assert _with_root_cert("host=h sslmode=verify-full") == (
        "host=h sslmode=verify-full sslrootcert=system"
    )
    assert _with_root_cert("host=h sslmode=verify-full sslrootcert=/ca.crt") == (
        "host=h sslmode=verify-full sslrootcert=/ca.crt"
    )
    assert _with_root_cert("host=localhost") == "host=localhost"


# --- a PostgreSQL catalog over TLS (CI service) ----------------------------------------------


def tls_service() -> tuple[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES_TLS")
    authority = os.environ.get("STORE_TEST_POSTGRES_TLS_CA")
    if not connection or not authority or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES_TLS and STORE_TEST_POSTGRES_TLS_CA are required in CI")
        pytest.skip("no PostgreSQL with TLS configured for this run")
    return connection, authority


def test_a_remote_catalog_over_verified_tls_works(tmp_path: Path) -> None:
    connection, authority = tls_service()
    location = postgres(f"{connection} sslmode=verify-full sslrootcert='{authority}'", tmp_path)
    lake = open_lake(location, LIMITS)
    try:
        assert write_and_read(lake) == [{"amount": "42.00"}]
    finally:
        lake.close()


def test_the_system_authorities_do_not_trust_a_private_certificate(tmp_path: Path) -> None:
    connection, _ = tls_service()
    with pytest.raises(StoreError) as refused:
        open_lake(postgres(f"{connection} sslmode=verify-full", tmp_path), LIMITS)
    assert refused.value.code == "STORE_CATALOG_UNAVAILABLE"
    detail = str(refused.value.details).lower()
    # libpq understood `sslrootcert=system` (16 or later) and checked the certificate: it did
    # not look for a file named "system".
    assert 'root certificate file "system"' not in detail
    assert "certificate" in detail


def test_verify_full_fails_closed_without_tls(tmp_path: Path) -> None:
    plain = os.environ.get("STORE_TEST_POSTGRES")
    tls_service()
    assert plain is not None
    without_tls = plain.replace("host=localhost", "host=127.0.0.2")
    with pytest.raises(StoreError) as refused:
        open_lake(postgres(f"{without_tls} sslmode=verify-full", tmp_path), LIMITS)
    assert refused.value.code == "STORE_CATALOG_UNAVAILABLE"
    assert os.environ[PASSWORD_ENV] not in str(refused.value.details)


# --- file catalogs ---------------------------------------------------------------------------


def test_a_duckdb_catalog_outside_the_lake_folder(tmp_path: Path) -> None:
    catalog = tmp_path / "elsewhere" / "my.ducklake"
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        catalog_path=str(catalog),
    )
    lake = open_lake(location, LIMITS)
    try:
        assert write_and_read(lake) == [{"amount": "42.00"}]
    finally:
        lake.close()
    assert catalog.is_file()
    assert not (tmp_path / "lake" / "catalog.ducklake").exists()
    reopened = open_lake(location, LIMITS)  # the same lake, found again
    try:
        assert reopened.current_snapshot().id >= 1
    finally:
        reopened.close()


def test_a_sqlite_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog" / "lake.sqlite"
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(catalog),
        data_path=(tmp_path / "data").as_posix() + "/",
    )
    lake = open_lake(location, LIMITS)
    try:
        assert write_and_read(lake) == [{"amount": "42.00"}]
        # The sandbox gains the catalog file and its companions, and not the catalog's folder.
        settings = lake._connection.execute(
            "SELECT current_setting('allowed_paths'), current_setting('allowed_directories')"
        ).fetchone()
        named = catalog.resolve().as_posix()
        assert settings is not None
        paths, directories = settings
        assert {named, f"{named}-wal", f"{named}-shm", f"{named}-journal"} <= set(paths)
        folder = catalog.parent.resolve().as_posix()
        assert not any(folder.startswith(entry.rstrip("/")) for entry in directories)
    finally:
        lake.close()
    assert catalog.read_bytes()[:16] == b"SQLite format 3\x00"
