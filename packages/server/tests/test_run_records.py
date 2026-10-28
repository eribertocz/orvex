"""P1-10: every run leaves a record per model, also when it fails; the runs command lists them."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import subprocess
import zoneinfo
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.run_record.v1 import RunRecord
from orvex.server import commands
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.store.errors import StoreError
from orvex.store.lake import _typed as typed
from orvex.transform.run_records import history_request, record_statements

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  archive:\n    type: parquet\n    path: data/r.parquet\n"


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def project(folder: Path, models: dict[str, str]) -> Path:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT range AS id, range * 1.5::DECIMAL(10,2) AS kwh FROM range(5)) "
        f"TO '{folder / 'data' / 'r.parquet'}'"
    )
    return write(folder, {"project.yaml": PROJECT, "connections.yaml": CONNECTIONS, **models})


def git(folder: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(folder),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.invalid",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def records(folder: Path) -> list[dict[str, object]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    cursor = lake.execute(
        "SELECT run_id, model, outcome, CAST(rows_written AS VARCHAR) AS rows_written, "
        "snapshot_id, project_commit, project_dirty, error_code, error_message, versions, "
        "CAST(epoch_us(started_at) AS VARCHAR) AS started_us "
        "FROM lake._meta.run_records "
        "ORDER BY min(started_at) OVER (PARTITION BY run_id), model"
    )
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    lake.close()
    return rows


MODELS = {
    "models/s/a.sql": "SELECT id, kwh FROM archive.data",
    "models/s/b.sql": "SELECT id FROM s.a",
    "models/s/c.sql": "SELECT count(*) AS n FROM s.b",
}


# --- without a lake --------------------------------------------------------------------------


def test_record_statements_bind_every_value() -> None:
    record = RunRecord.model_validate(
        json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "contracts/run-record/v1/examples/valid/failed.json"
            ).read_text(encoding="utf-8")
        )
    )
    statements = record_statements([record])
    insert = statements[-1]
    assert "Conversion" not in insert.sql  # values never enter the SQL text
    # Every parameter is found and typed the way the store binds it.
    assert typed(insert.sql, list(insert.parameters)).count("CAST($p_") == len(insert.parameters)
    values = {parameter.name: parameter.value for parameter in insert.parameters}
    assert values["p_error_code"] == "STORE_QUERY_FAILED"
    assert values["p_rows_written"] is None
    assert values["p_project_dirty"] == "false"


def test_history_parameters_bind() -> None:
    request = history_request(1, "America/La_Paz", 5)
    assert typed(request.sql, list(request.parameters)).count("CAST($p_") == 2


def test_runs_without_any_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MODELS)
    assert command(capsys, "runs", str(tmp_path)) == (0, "no runs yet\n")


# --- with a lake (DuckLake extension, CI) --------------------------------------------------------


def test_successful_run_records_every_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MODELS)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "models")
    head = git(tmp_path, "rev-parse", "HEAD")
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    run_id = re.search(r"^run (\S+)$", out, re.MULTILINE)
    snapshot = re.search(r"^snapshot (\d+)$", out, re.MULTILINE)
    assert run_id
    assert snapshot
    rows = records(tmp_path)
    assert [(r["model"], r["outcome"], r["rows_written"]) for r in rows] == [
        ("s.a", "success", "5"),
        ("s.b", "success", "5"),
        ("s.c", "success", "1"),
    ]
    assert {r["run_id"] for r in rows} == {run_id.group(1)}
    assert {r["snapshot_id"] for r in rows} == {int(snapshot.group(1))}
    assert {r["project_commit"] for r in rows} == {head}
    assert {r["project_dirty"] for r in rows} == {False}
    versions = json.loads(str(rows[0]["versions"]))
    assert versions["duckdb"] == duckdb.__version__

    write(tmp_path, {"models/s/c.sql": "SELECT count(*) AS n, 1 AS one FROM s.b"})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert {r["project_dirty"] for r in records(tmp_path)[3:]} == {True}


def test_failed_run_is_recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(
        tmp_path,
        {
            "models/s/a.sql": "SELECT id FROM archive.data",
            "models/s/b.sql": "SELECT CAST('x' AS INTEGER) AS n FROM s.a",
            "models/s/c.sql": "SELECT count(*) AS n FROM s.b",
        },
    )
    code, _out = command(capsys, "run", str(tmp_path))
    assert code == 1
    rows = records(tmp_path)
    assert [(r["model"], r["outcome"], r["snapshot_id"]) for r in rows] == [
        ("s.a", "failed", None),
        ("s.b", "failed", None),
        ("s.c", "not_run", None),
    ]
    assert rows[0]["error_code"] is None
    assert rows[1]["error_code"] == "STORE_QUERY_FAILED"
    assert "convert" in str(rows[1]["error_message"]).lower()
    assert {r["project_commit"] for r in rows} == {None}  # not a git repository
    lake = duckdb.connect()
    lake.execute("LOAD ducklake")
    catalog = tmp_path / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    tables = lake.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 's'"
    ).fetchall()
    lake.close()
    assert tables == [(0,)]


def test_runs_lists_newest_first_in_the_project_time_zone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MODELS)
    first = command(capsys, "run", str(tmp_path))[1]
    second = command(capsys, "run", str(tmp_path))[1]
    ids = [re.search(r"^run (\S+)$", out, re.MULTILINE).group(1) for out in (first, second)]  # type: ignore[union-attr]
    code, out = command(capsys, "runs", str(tmp_path))
    assert code == 0
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == [ids[1], ids[0]]
    assert "success" in lines[0]
    assert "s.a, s.b, s.c" in lines[0]
    started_us = min(int(str(r["started_us"])) for r in records(tmp_path) if r["run_id"] == ids[1])
    local = dt.datetime.fromtimestamp(started_us / 1_000_000, zoneinfo.ZoneInfo("America/La_Paz"))
    assert local.strftime("%Y-%m-%d %H:%M:%S") in lines[0]
    code, out = command(capsys, "runs", str(tmp_path), "--limit", "1")
    assert out.splitlines()[0].split()[0] == ids[1]
    assert len(out.splitlines()) == 1


def test_password_never_reaches_a_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "never-in-a-record-7Q"
    monkeypatch.setenv("RUN_RECORD_TEST_PASSWORD", secret)
    project(tmp_path, MODELS)
    write(
        tmp_path,
        {
            "connections.yaml": CONNECTIONS
            + "  production:\n    type: postgres\n    host: 127.0.0.1\n    port: 1\n"
            "    database: x\n    user: y\n    password: ${env:RUN_RECORD_TEST_PASSWORD}\n"
            "    read_only: true\n",
        },
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert secret not in out
    rows = records(tmp_path)
    assert rows
    assert {r["outcome"] for r in rows} == {"failed"}
    assert {r["error_code"] for r in rows} == {"STORE_CONNECTION_FAILED"}
    assert secret not in json.dumps(rows, default=str)


# --- failure paths, with a stand-in lake ---------------------------------------------------------


class FakeTransaction:
    def __init__(self, lake: FakeLake) -> None:
        self.lake = lake

    def execute(self, request: object) -> None:
        self.lake.executed.append(request)


class FakeLake:
    """Fails where a test asks: when a transaction starts, commits, or when counting."""

    def __init__(self, fail: str) -> None:
        self.fail = fail
        self.executed: list[object] = []
        self.transactions = 0

    @contextlib.contextmanager
    def transaction(self) -> Iterator[FakeTransaction]:
        self.transactions += 1
        first = self.transactions == 1
        if first and self.fail == "start":
            raise StoreError("STORE_BUSY", "The lake is busy.", {})
        yield FakeTransaction(self)
        if first and self.fail == "commit":
            raise StoreError("STORE_TRANSACTION_CONFLICT", "Another writer changed it.", {})

    def current_snapshot(self) -> object:
        if self.fail == "count":
            raise StoreError("STORE_TIMEOUT", "The statement exceeded its time limit.", {})
        raise AssertionError("not reached in these tests")

    def read(self, request: object) -> object:
        raise AssertionError("not reached in these tests")

    def close(self) -> None:
        pass


def recorded(lake: FakeLake) -> list[dict[str, str | None]]:
    rows = []
    for request in lake.executed:
        sql = getattr(request, "sql", "")
        if sql.startswith("INSERT INTO _meta.run_records"):
            rows.append({p.name: p.value for p in getattr(request, "parameters", [])})
    return rows


@pytest.mark.parametrize(
    ("fail", "code"),
    [("start", "STORE_BUSY"), ("commit", "STORE_TRANSACTION_CONFLICT")],
)
def test_run_failures_no_model_caused_are_recorded_with_their_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fail: str,
    code: str,
) -> None:
    project(tmp_path, MODELS)
    lake = FakeLake(fail)
    monkeypatch.setattr(commands, "open_lake", lambda *args, **kwargs: lake)
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == 1
    assert code in out
    rows = recorded(lake)
    assert [(r["p_model"], r["p_outcome"], r["p_error_code"]) for r in rows] == [
        ("s.a", "failed", code),
        ("s.b", "failed", code),
        ("s.c", "failed", code),
    ]


def test_a_committed_run_is_recorded_even_if_counting_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path, MODELS)
    lake = FakeLake("count")
    monkeypatch.setattr(commands, "open_lake", lambda *args, **kwargs: lake)
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == 0
    assert "STORE_TIMEOUT" in out
    rows = recorded(lake)
    assert [(r["p_outcome"], r["p_rows_written"]) for r in rows] == [("success", None)] * 3


def test_runs_limit_must_be_positive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MODELS)
    assert main(["runs", str(tmp_path), "--limit", "0"]) == 2
