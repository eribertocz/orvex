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

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import (
    AnyLakeLocation,
    Lake,
    Limits,
    _conninfo,
    _inline_secret,
    _require_tls,
    _sandbox_entries,
    _with_root_cert,
    open_lake,
)

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


def test_a_password_is_a_keyword_not_a_word() -> None:
    assert _inline_secret("host=h password=x")
    assert _inline_secret("host=h PASSWORD = x")
    assert _inline_secret("host=h sslpassword=x")
    assert _inline_secret("postgresql://u:x@h/d")
    # Not key=value pairs (an unterminated quote): judged by the word alone.
    assert _inline_secret("host='broken password=x")
    assert not _inline_secret("host='broken")
    assert not _inline_secret("host='h' dbname='password_vault' user='passwords'")


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


def closed(lake: Lake, path: Path) -> None:
    """The lake's own DuckDB connection may not open `path`: the sandbox refuses it (the
    statement guard, which refuses file readers anyway, is not involved)."""
    with pytest.raises(duckdb.PermissionException):
        lake._connection.execute(f"SELECT count(*) FROM read_csv('{path.as_posix()}')").fetchone()


def only_listed_entries(lake: Lake, location: AnyLakeLocation) -> None:
    """The connection may open exactly the store's own list, plus what DuckDB adds by itself
    (measured on DuckDB 2.0.0): for each database it attached, the path and its `.wal`,
    `.wal.checkpoint` and `.wal.recovery`, with the path as listed, without a `type:` prefix
    (`sqlite:/…`), and read relative to the working folder; and its temporary folder."""
    folders, files = _sandbox_entries(location, lake._spill)
    settings = lake._connection.execute(
        "SELECT current_setting('allowed_paths'), current_setting('allowed_directories'), "
        "current_setting('temp_directory')"
    ).fetchone()
    assert settings is not None
    paths, directories, temporary = settings
    attached = [
        str(path)
        for (path,) in lake._connection.execute(
            "SELECT path FROM duckdb_databases() WHERE path IS NOT NULL"
        ).fetchall()
    ]
    forms = set()
    for path in attached:
        forms.add(path)
        if ":" in path:  # `sqlite:/…`: DuckDB also lists it bare and relative to the folder
            forms.add(path.split(":", 1)[1])
            forms.add(f"{Path.cwd().as_posix()}/{path}")
    by_duckdb = {
        f"{form}{suffix}"
        for form in forms
        for suffix in ("", ".wal", ".wal.checkpoint", ".wal.recovery")
    }
    assert set(files) <= set(paths)
    assert set(paths) - set(files) <= by_duckdb, set(paths) - set(files)
    listed = {folder.rstrip("/") + "/" for folder in folders}
    assert listed <= set(directories)
    assert set(directories) - listed <= {str(temporary).rstrip("/") + "/"}


@pytest.mark.parametrize("catalog_path", [None, "elsewhere/my.ducklake"])
def test_the_sandbox_lists_exactly_the_lake_and_its_catalog(
    tmp_path: Path, catalog_path: str | None
) -> None:
    lake = (tmp_path / "lake").resolve().as_posix()
    local = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        catalog_path=None if catalog_path is None else str(tmp_path / catalog_path),
    )
    files = []
    if catalog_path is not None:
        named = (tmp_path / catalog_path).resolve().as_posix()
        files = [named, f"{named}.wal"]
    assert _sandbox_entries(local, "/spill", ["/srv/a.csv"]) == (
        ["/spill", lake],
        ["/srv/a.csv", *files],
    )


def test_the_sandbox_lists_exactly_a_sqlite_catalog_and_its_companions(tmp_path: Path) -> None:
    catalog = (tmp_path / "catalog" / "lake.sqlite").resolve().as_posix()
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(tmp_path / "catalog" / "lake.sqlite"),
        data_path=(tmp_path / "data").as_posix() + "/",
    )
    assert _sandbox_entries(location, "/spill") == (
        ["/spill", (tmp_path / "data").resolve().as_posix()],
        [catalog, f"{catalog}-wal", f"{catalog}-shm", f"{catalog}-journal"],
    )


def test_the_sandbox_lists_no_catalog_file_for_postgres(tmp_path: Path) -> None:
    location = PostgresLakeLocation(
        kind="lake_location",
        catalog="postgres",
        connection="host=localhost dbname=lake user=lake",
        password_env=PASSWORD_ENV,
        metadata_schema="lake",
        data_path=(tmp_path / "data").as_posix() + "/",
    )
    assert _sandbox_entries(location, "/spill") == (
        ["/spill", (tmp_path / "data").resolve().as_posix()],
        [],
    )


def test_a_duckdb_catalog_outside_the_lake_folder(tmp_path: Path) -> None:
    catalog = tmp_path / "elsewhere" / "my.ducklake"
    location = LocalLakeLocation(
        kind="lake_location",
        catalog="local",
        path=str(tmp_path / "lake"),
        catalog_path=str(catalog),
    )
    beside = tmp_path / "elsewhere" / "beside.csv"
    beside.parent.mkdir(parents=True)
    beside.write_text("x\n1\n", encoding="utf-8")
    lake = open_lake(location, LIMITS)
    try:
        assert write_and_read(lake) == [{"amount": "42.00"}]
        # The sandbox gains the catalog file and its write-ahead log, not their folder.
        only_listed_entries(lake, location)
        closed(lake, beside)
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
    beside = tmp_path / "catalog" / "beside.csv"
    beside.parent.mkdir(parents=True)
    beside.write_text("x\n1\n", encoding="utf-8")
    lake = open_lake(location, LIMITS)
    try:
        assert write_and_read(lake) == [{"amount": "42.00"}]
        # The sandbox gains the catalog file and its companions, and not the catalog's folder:
        # a file beside the catalog stays closed.
        only_listed_entries(lake, location)
        closed(lake, beside)
    finally:
        lake.close()
    assert catalog.read_bytes()[:16] == b"SQLite format 3\x00"
