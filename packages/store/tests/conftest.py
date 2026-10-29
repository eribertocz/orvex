"""Lake fixtures: every lake test runs on the local catalog and on the PostgreSQL catalog; the
shared contract tests (`test_lake.py`, `test_maintenance.py`) also run on a SQLite catalog.

The PostgreSQL catalog needs `STORE_TEST_POSTGRES` (a libpq connection string without password)
and `STORE_TEST_POSTGRES_PASSWORD`. In CI they are required; elsewhere the PostgreSQL variant is
skipped when they are missing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.contracts.store.v1 import LocalLakeLocation, PostgresLakeLocation, SqliteLakeLocation
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
)
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"


def make_postgres_location(tmp_path: Path) -> PostgresLakeLocation:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES and STORE_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL catalog configured for this run")
    return PostgresLakeLocation(
        kind="lake_location",
        catalog="postgres",
        connection=connection,
        password_env=PASSWORD_ENV,
        metadata_schema=f"t_{uuid.uuid4().hex[:16]}",
        data_path=(tmp_path / "data").as_posix() + "/",
    )


def local_location(tmp_path: Path) -> LocalLakeLocation:
    return LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path))


def sqlite_location(tmp_path: Path) -> SqliteLakeLocation:
    return SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(tmp_path / "catalog.sqlite"),
        data_path=(tmp_path / "data").as_posix() + "/",
    )


type AnyLocation = LocalLakeLocation | PostgresLakeLocation | SqliteLakeLocation


def make_location(kind: str, tmp_path: Path) -> AnyLocation:
    if kind == "postgres":
        return make_postgres_location(tmp_path)
    if kind == "sqlite":
        return sqlite_location(tmp_path)
    return local_location(tmp_path)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Each lake test runs on the local and the PostgreSQL catalog; a module that sets
    `EVERY_CATALOG = True` (the shared contract tests) also runs on SQLite (ADR-0027 §2)."""
    if "location" in metafunc.fixturenames:
        kinds = ["local", "postgres"]
        if getattr(metafunc.module, "EVERY_CATALOG", False):
            kinds.append("sqlite")
        metafunc.parametrize("location", kinds, indirect=True)


@pytest.fixture
def location(request: pytest.FixtureRequest, tmp_path: Path) -> AnyLocation:
    return make_location(request.param, tmp_path)


@pytest.fixture
def postgres_location(tmp_path: Path) -> PostgresLakeLocation:
    return make_postgres_location(tmp_path)


@pytest.fixture
def lake(location: AnyLocation) -> Iterator[Lake]:
    opened = open_lake(location, LIMITS)
    yield opened
    opened.close()
