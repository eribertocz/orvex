"""#436, #437: `<slug> admin`, end to end on PostgreSQL: the first administrator, the recovery
file and the refusals, each recorded in the audit log; `admin audit verify`, `retain` and
`rotate-key`; and no store at all when PostgreSQL is not configured."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.access.audit import open_audit_log
from orvex.access.authentication.service import SignIn
from orvex.server.access_store import access_store, postgres_variables
from orvex.server.audit_settings import audit_settings
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable

from tests.support.access_directory import access_role, audit_events, postgres_connection

IDENTITY = load_identity()
PREFIX = IDENTITY.slug.upper().replace("-", "_")


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path))
    for variable in (*postgres_variables(IDENTITY), f"{PREFIX}_AUDIT_RETENTION_DAYS"):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


@pytest.fixture
def server_instance(instance: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with access_role(*postgres_connection()) as (connection, password):
        variable, password_variable = postgres_variables(IDENTITY)
        monkeypatch.setenv(variable, connection)
        monkeypatch.setenv(password_variable, password)
        yield instance


def create(*extra: str, name: str = "root") -> list[str]:
    return ["admin", "create", "--user-name", name, "--display-name", name.title(), *extra]


def test_without_postgres_the_command_is_refused_and_writes_nothing(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(create()) == 1
    assert "ACCESS_STORE_NOT_CONFIGURED" in capsys.readouterr().err
    assert sorted(path.name for path in instance.iterdir()) == []


def test_create_then_refuse_then_recover_each_audited(
    server_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(create()) == 0
    out, err = capsys.readouterr()
    recovery = server_instance / "access-recovery.key"
    assert f"recovery file written: {recovery}" in out
    assert "administrator created:" in out
    secret = recovery.read_text("ascii").strip()
    assert secret not in out + err
    assert (server_instance / "audit-signing.key").stat().st_mode & 0o777 == 0o600

    assert main(create(name="second")) == 1
    assert "ACCESS_FORBIDDEN" in capsys.readouterr().err

    moved = server_instance / f"{uuid.uuid4().hex}.key"
    recovery.rename(moved)
    assert main(create("--recover", name="third")) == 1  # the file is not where it was
    assert main(create("--recover", "--recovery-file", str(moved), name="third")) == 0
    assert "recovery file written" not in capsys.readouterr().out  # never written again

    # Two creations and two refusals.
    assert main(["admin", "audit", "verify"]) == 0
    assert "audit log verified: 4 events" in capsys.readouterr().out
    anchors = server_instance / "audit-anchors.log"
    assert main(["admin", "audit", "verify", "--anchors", str(anchors)]) == 0


def test_rotate_then_verify_then_retain(
    server_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(create()) == 0
    key = server_instance / "audit-signing.key"
    before = key.read_bytes()
    assert main(["admin", "audit", "rotate-key"]) == 0
    assert "audit signing key rotated: sha256:" in capsys.readouterr().out
    assert key.read_bytes() != before
    assert not key.with_name(key.name + ".new").exists()
    assert main(["admin", "audit", "verify"]) == 0
    assert main(["admin", "audit", "retain"]) == 0
    assert "audit retention: nothing to remove" in capsys.readouterr().out


def test_a_retention_setting_below_30_days_is_refused(
    server_instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{PREFIX}_AUDIT_RETENTION_DAYS", "29")
    assert main(["admin", "audit", "retain"]) == 1
    assert "AUDIT_RETENTION_INVALID" in capsys.readouterr().err


def test_the_first_administrator_signs_in_once_with_the_printed_temporary_password(
    server_instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#438: the password is printed once, never logged, and opens a session that may only
    change it."""
    assert main(create()) == 0
    out = capsys.readouterr().out
    (line,) = [line for line in out.splitlines() if line.startswith("temporary password")]
    temporary = line.rsplit(" ", 1)[1]
    store = access_store("server", IDENTITY, server_instance, os.environ)
    try:
        settings = audit_settings(IDENTITY, server_instance, os.environ)
        log = open_audit_log(store, settings.key, settings.anchors)
        signed = SignIn(store, log).login("root", temporary, "127.0.0.1")
        assert signed.password_change_only
        assert temporary not in json.dumps(audit_events(store))
    finally:
        store.close()
