"""#436: `<slug> admin create`, end to end on PostgreSQL: the first administrator, the recovery
file, the refusals, and no SQLite store when PostgreSQL is not configured."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.server.access_store import postgres_variables
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable

from tests.support.access_directory import postgres_connection, superuser

IDENTITY = load_identity()


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path))
    for variable in postgres_variables(IDENTITY):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


@pytest.fixture
def server_instance(instance: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    connection, password = postgres_connection()
    variable, password_variable = postgres_variables(IDENTITY)
    monkeypatch.setenv(variable, connection)
    monkeypatch.setenv(password_variable, password)
    yield instance
    admin = superuser(connection, password)
    try:
        admin.run("DROP SCHEMA IF EXISTS access CASCADE")
    finally:
        admin.close()


def create(*extra: str, name: str = "root") -> list[str]:
    return ["admin", "create", "--user-name", name, "--display-name", name.title(), *extra]


def test_without_postgres_the_command_is_refused_and_writes_nothing(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(create()) == 1
    assert "ACCESS_STORE_NOT_CONFIGURED" in capsys.readouterr().err
    assert sorted(path.name for path in instance.iterdir()) == []


def test_create_then_refuse_then_recover(
    server_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(create()) == 0
    out, err = capsys.readouterr()
    recovery = server_instance / "access-recovery.key"
    assert f"recovery file written: {recovery}" in out
    assert "administrator created:" in out
    assert "audit: admin_create done user=" in err
    secret = recovery.read_text("ascii").strip()
    assert secret not in out + err

    assert main(create(name="second")) == 1
    assert "ACCESS_FORBIDDEN" in capsys.readouterr().err

    moved = server_instance / f"{uuid.uuid4().hex}.key"
    recovery.rename(moved)
    assert main(create("--recover", name="third")) == 1  # the file is not where it was
    assert "audit: admin_recover refused code=ACCESS_FORBIDDEN" in capsys.readouterr().err
    assert main(create("--recover", "--recovery-file", str(moved), name="third")) == 0
    assert "recovery file written" not in capsys.readouterr().out  # never written again
