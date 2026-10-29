"""#122: PostgreSQL sources on another machine need `ssl_mode: verify-full`.

Validation tests run anywhere. The run test needs the TLS service of #120's CI
(`STORE_TEST_POSTGRES_TLS`, `STORE_TEST_POSTGRES_TLS_CA`) and the DuckLake extension.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"


def project(folder: Path, connection: str, model: str = "") -> Path:
    files = {
        "project.yaml": PROJECT,
        "connections.yaml": f"version: 1\nconnections:\n  production:\n{connection}",
    }
    if model:
        files["models/s/copied.sql"] = model
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def postgres(host: str, port: int = 5432, extra: str = "") -> str:
    return (
        f"    type: postgres\n    host: {host}\n    port: {port}\n    database: postgres\n"
        f"    user: postgres\n    password: ${{env:{PASSWORD_ENV}}}\n    read_only: true\n{extra}"
    )


def validate(capsys: pytest.CaptureFixture[str], folder: Path) -> tuple[int, str]:
    code = main(["validate", str(folder)])
    return code, capsys.readouterr().out


def test_a_remote_source_without_verify_full_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "secret-value")
    for extra in ["", "    ssl_mode: disable\n"]:
        project(tmp_path, postgres("127.0.0.2", extra=extra))
        code, out = validate(capsys, tmp_path)
        assert code == 1
        assert out.startswith("SOURCE_TLS_REQUIRED connections.yaml")
        assert "'production'" in out
        assert "secret-value" not in out
    assert not (tmp_path / f".{SLUG}").exists()


@pytest.mark.parametrize("mode", ["require", "verify-ca", "prefer", "allow"])
def test_other_ssl_modes_are_schema_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    project(tmp_path, postgres("localhost", extra=f"    ssl_mode: {mode}\n"))
    code, out = validate(capsys, tmp_path)
    assert code == 1
    assert out.startswith("PROJECT_SCHEMA_INVALID connections.yaml")


def test_a_local_source_with_verify_full_or_without_ssl_mode_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for extra in ["", "    ssl_mode: disable\n", "    ssl_mode: verify-full\n"]:
        project(tmp_path, postgres("localhost", extra=extra))
        code, out = validate(capsys, tmp_path)
        assert code == 0, out


# --- a run over verified TLS (CI) ------------------------------------------------------------


def tls_service() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES_TLS")
    if not connection or not os.environ.get("STORE_TEST_POSTGRES_TLS_CA"):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES_TLS and STORE_TEST_POSTGRES_TLS_CA are required in CI")
        pytest.skip("no PostgreSQL with TLS configured for this run")
    return dict(item.split("=", 1) for item in connection.split())


@pytest.fixture
def readings() -> Iterator[str]:
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


def test_a_model_reads_a_remote_source_over_verified_tls(
    readings: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = tls_service()
    monkeypatch.setenv("SOURCE_TEST_CA", os.environ["STORE_TEST_POSTGRES_TLS_CA"])
    assert settings["host"] not in {"localhost", "127.0.0.1", "::1"}  # remote for the rule
    project(
        tmp_path,
        postgres(
            settings["host"],
            int(settings["port"]),
            "    ssl_mode: verify-full\n    ssl_root_cert: ${env:SOURCE_TEST_CA}\n",
        ),
        f"SELECT * FROM production.{readings}.readings",
    )
    code = main(["run", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "built s.copied: 100 rows" in out
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = tmp_path / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    total = lake.execute("SELECT CAST(sum(kwh) AS VARCHAR) FROM lake.s.copied").fetchone()
    lake.close()
    assert total == ("631.2500",)  # the rows match the source
