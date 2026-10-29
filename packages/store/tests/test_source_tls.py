"""#122: a PostgreSQL source on another machine is read only over verified TLS.

The rule tests run anywhere (the refusal comes before anything is opened). The service tests
need `STORE_TEST_POSTGRES_TLS` (a PostgreSQL with TLS, reached by a host other than the local
machine) and `STORE_TEST_POSTGRES_TLS_CA` (its certificate authority's file), from #120's CI.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresSourceAttachment,
    ReadRequest,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Limits, _require_source_tls, _source_tls, open_lake

LIMITS = Limits(
    memory_limit="512MB", threads=1, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"


def source(host: str, port: int = 5432, **tls: str) -> PostgresSourceAttachment:
    return PostgresSourceAttachment.model_validate(
        {
            "kind": "source_attachment",
            "name": "production",
            "type": "postgres",
            "host": host,
            "port": port,
            "database": "postgres",
            "user": "postgres",
            "password_env": PASSWORD_ENV,
            **tls,
        }
    )


def lake_location(tmp_path: Path) -> LocalLakeLocation:
    return LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path / "lake"))


# --- the rule --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attachment",
    [
        source("127.0.0.2"),
        source("db.example.com"),
        source("db.example.com", sslmode="disable"),  # disable is for the local machine only
    ],
)
def test_a_remote_source_without_verify_full_is_refused(
    attachment: PostgresSourceAttachment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "not-used")
    with pytest.raises(StoreError) as refused:
        open_lake(lake_location(tmp_path), LIMITS, [attachment])
    assert refused.value.code == "STORE_TLS_REQUIRED"
    assert refused.value.details["source"] == "production"
    assert "not-used" not in str(refused.value.details)
    assert not (tmp_path / "lake").exists()  # refused before anything was opened


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1"])
def test_local_sources_need_no_tls(host: str) -> None:
    _require_source_tls(source(host))
    _require_source_tls(source(host, sslmode="disable"))
    assert _source_tls(source(host)) == ""
    assert _source_tls(source(host, sslmode="disable")) == " sslmode='disable'"


def test_verify_full_uses_the_system_authorities_or_the_named_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _source_tls(source("db.example.com", sslmode="verify-full")) == (
        " sslmode='verify-full' sslrootcert='system'"
    )
    monkeypatch.setenv("SOURCE_TEST_CA", "/etc/ssl/it's ca.crt")
    assert _source_tls(
        source("db.example.com", sslmode="verify-full", sslrootcert="${env:SOURCE_TEST_CA}")
    ) == (" sslmode='verify-full' sslrootcert='/etc/ssl/it\\'s ca.crt'")


def test_an_unset_authority_variable_is_refused_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "not-used")
    monkeypatch.delenv("SOURCE_TEST_CA_UNSET", raising=False)
    attachment = source(
        "db.example.com", sslmode="verify-full", sslrootcert="${env:SOURCE_TEST_CA_UNSET}"
    )
    with pytest.raises(StoreError) as refused:
        open_lake(lake_location(tmp_path), LIMITS, [attachment])
    assert refused.value.code == "STORE_SECRET_MISSING"
    assert refused.value.details["variable"] == "SOURCE_TEST_CA_UNSET"
    assert not (tmp_path / "lake").exists()


# --- the TLS service (CI) --------------------------------------------------------------------


def tls_service() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES_TLS")
    if not connection or not os.environ.get("STORE_TEST_POSTGRES_TLS_CA"):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES_TLS and STORE_TEST_POSTGRES_TLS_CA are required in CI")
        pytest.skip("no PostgreSQL with TLS configured for this run")
    return dict(item.split("=", 1) for item in connection.split())


@pytest.fixture
def readings() -> Iterator[str]:
    """A schema with a readings table on the TLS service, prepared over the local machine."""
    settings = tls_service()
    admin = duckdb.connect()
    admin.install_extension("postgres")
    admin.load_extension("postgres")
    password = os.environ[PASSWORD_ENV].replace("'", "''")
    admin.sql(f"CREATE SECRET (TYPE postgres, PASSWORD '{password}')")
    admin.sql(
        f"ATTACH 'host=localhost port={settings['port']} dbname={settings['dbname']} "
        f"user={settings['user']}' AS pg (TYPE postgres)"
    )
    name = f"src_{uuid.uuid4().hex[:12]}"
    admin.sql(f"CALL postgres_execute('pg', 'CREATE SCHEMA {name}')")
    admin.sql(
        f"CALL postgres_execute('pg', 'CREATE TABLE {name}.readings "
        "(id bigint, kwh numeric(18,4))')"
    )
    admin.sql(
        f"CALL postgres_execute('pg', 'INSERT INTO {name}.readings "
        "SELECT i, i / 8.0 FROM generate_series(1, 100) AS i')"
    )
    try:
        yield name
    finally:
        admin.sql(f"CALL postgres_execute('pg', 'DROP SCHEMA {name} CASCADE')")
        admin.close()


def test_a_remote_source_over_verified_tls_is_read(
    readings: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tls_service()
    monkeypatch.setenv("SOURCE_TEST_CA", os.environ["STORE_TEST_POSTGRES_TLS_CA"])
    attachment = source(
        settings["host"],
        int(settings["port"]),
        sslmode="verify-full",
        sslrootcert="${env:SOURCE_TEST_CA}",
    )
    lake = open_lake(lake_location(tmp_path), LIMITS, [attachment])
    try:
        with lake.transaction() as transaction:
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql=f"CREATE TABLE copied AS SELECT * FROM production.{readings}.readings",
                    parameters=[],
                )
            )
        request = ReadRequest(
            kind="read",
            sql="SELECT count(*) AS n, CAST(sum(kwh) AS VARCHAR) AS total FROM copied",
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=16,
        )
        assert lake.read(request).read_all().to_pylist() == [{"n": 100, "total": "631.2500"}]
    finally:
        lake.close()


def test_verify_full_against_a_server_without_tls_fails_closed(tmp_path: Path) -> None:
    tls_service()
    plain = dict(item.split("=", 1) for item in os.environ["STORE_TEST_POSTGRES"].split())
    attachment = source("127.0.0.2", int(plain.get("port", "5432")), sslmode="verify-full")
    with pytest.raises(StoreError) as refused:
        open_lake(lake_location(tmp_path), LIMITS, [attachment])
    assert refused.value.code == "STORE_CONNECTION_FAILED"
    assert os.environ[PASSWORD_ENV] not in str(refused.value)
    assert os.environ[PASSWORD_ENV] not in str(refused.value.details)
