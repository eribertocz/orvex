"""#436: the instance's access store is chosen by its mode alone (Owner, 2026-11-10): SQLite in
local mode, PostgreSQL always in server mode, never a silent fallback."""

from __future__ import annotations

from pathlib import Path

import pytest
from orvex.access.directory import PostgresStore, SqliteStore
from orvex.access.errors import VALIDATION_FAILED, AccessError
from orvex.server.access_store import (
    ACCESS_STORE_NOT_CONFIGURED,
    access_store,
    postgres_variables,
)
from orvex.server.identity import load_identity

IDENTITY = load_identity()
CONNECTION, PASSWORD = postgres_variables(IDENTITY)


def test_the_variables_come_from_the_identity_slug() -> None:
    prefix = IDENTITY.slug.upper().replace("-", "_")
    assert (
        f"{prefix}_ACCESS_POSTGRES",
        f"{prefix}_ACCESS_POSTGRES_PASSWORD",
    ) == (CONNECTION, PASSWORD)


def test_local_mode_uses_sqlite_in_the_instance_directory(tmp_path: Path) -> None:
    store = access_store("local", IDENTITY, tmp_path, {CONNECTION: "host=localhost"})
    assert isinstance(store, SqliteStore)
    assert store.path == tmp_path / "access.sqlite"


def test_server_mode_uses_postgres(tmp_path: Path) -> None:
    environment = {CONNECTION: "host=localhost dbname=bi user=access", PASSWORD: "secret"}
    store = access_store("server", IDENTITY, tmp_path, environment)
    assert isinstance(store, PostgresStore)
    assert store.settings.schema == "access"


@pytest.mark.parametrize(
    "environment",
    [{}, {CONNECTION: "host=localhost dbname=bi user=access"}, {PASSWORD: "secret"}],
    ids=["nothing", "no password", "no connection"],
)
def test_server_mode_without_postgres_is_refused_never_sqlite(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    with pytest.raises(AccessError) as refused:
        access_store("server", IDENTITY, tmp_path, environment)
    assert refused.value.code == ACCESS_STORE_NOT_CONFIGURED
    assert not (tmp_path / "access.sqlite").exists()


def test_server_mode_refuses_a_remote_server_without_verified_tls(tmp_path: Path) -> None:
    environment = {CONNECTION: "host=db.example.com dbname=bi user=access", PASSWORD: "s"}
    with pytest.raises(AccessError) as refused:
        access_store("server", IDENTITY, tmp_path, environment)
    assert refused.value.code == VALIDATION_FAILED
