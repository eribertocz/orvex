"""P1-10: every run leaves a record per model, also when it fails; the runs command lists them."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import zoneinfo
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import orvex.store.transaction as store_transaction
import pytest
from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import LocalLakeLocation, ReadRequest, StatementRequest
from orvex.server import commands, lake_open, maintenance
from orvex.server import run as run_command
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Transaction, open_lake
from orvex.transform.run_records import (
    history_request,
    last_run_request,
    model_history_request,
    record_statements,
)

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


def catalog(folder: Path) -> Path:
    return folder / f".{SLUG}" / "lake" / "catalog.ducklake"


def lake_rows(folder: Path, sql: str, *, writable: bool = False) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    mode = "" if writable else " (READ_ONLY)"
    lake.execute(f"ATTACH 'ducklake:{catalog(folder)}' AS lake{mode}")
    rows = lake.execute(sql).fetchall()
    lake.close()
    return rows


# A record's snapshot, resolved as the readers do (#394): the row's own snapshot for a success.
RESOLVED = "CASE WHEN outcome = 'success' THEN snapshot_id END"


def records(folder: Path) -> list[dict[str, object]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    lake.execute(f"ATTACH 'ducklake:{catalog(folder)}' AS lake (READ_ONLY)")
    cursor = lake.execute(
        "SELECT run_id, model, outcome, CAST(rows_written AS VARCHAR) AS rows_written, "
        f"{RESOLVED} AS snapshot_id, project_commit, project_dirty, error_code, error_message, "
        "versions, CAST(epoch_us(started_at) AS VARCHAR) AS started_us "
        "FROM lake._meta.run_records_v2 "
        "ORDER BY min(started_at) OVER (PARTITION BY run_id), model"
    )
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    lake.close()
    return rows


def snapshot_of_token(folder: Path, token: str) -> int:
    ((snapshot,),) = lake_rows(
        folder,
        f"SELECT snapshot_id FROM ducklake_snapshots('lake') WHERE commit_extra_info = '{token}'",
    )
    return int(str(snapshot))


def latest_snapshot(folder: Path) -> int:
    ((snapshot,),) = lake_rows(folder, "SELECT max(snapshot_id) FROM ducklake_snapshots('lake')")
    return int(str(snapshot))


def printed_snapshot(out: str) -> int:
    found = re.search(r"^snapshot (\d+)$", out, re.MULTILINE)
    assert found, out
    return int(found.group(1))


def runs_lines(capsys: pytest.CaptureFixture[str], folder: Path) -> list[str]:
    code, out = command(capsys, "runs", str(folder))
    assert code == 0, out
    return out.splitlines()[1:]  # the first line is the scheduler's state


def runs_snapshot(line: str) -> str:
    return line.split("snapshot ")[1].split()[0]


@contextlib.contextmanager
def open_project_lake(folder: Path) -> Iterator[Lake]:
    home = folder / f".{SLUG}"
    lake = commands.open_existing(home, commands.lake_location(load_project(folder), home))
    try:
        yield lake
    finally:
        lake.close()


def armed_on_records(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """A flag set once the run inserts its records: the next commit is the run's own."""
    armed: list[bool] = []
    real = record_statements

    def arming(records: object) -> list[StatementRequest]:
        armed.append(True)
        return real(records)  # type: ignore[arg-type]

    monkeypatch.setattr(run_command, "record_statements", arming)
    return armed


MODELS = {
    "models/s/a.sql": "SELECT id, kwh FROM archive.data",
    "models/s/b.sql": "SELECT id FROM s.a",
    "models/s/c.sql": "SELECT count(*) AS n FROM s.b",
}


# --- without a lake --------------------------------------------------------------------------


BOTH = {("_meta", "run_records"), ("_meta", "run_records_v2")}


def test_a_lake_without_record_tables_has_no_record_reads() -> None:
    others = {("s", "a"), ("_meta", "watermarks")}
    assert history_request(1, "UTC", 5, others) is None
    assert model_history_request(1, others) is None
    assert last_run_request(1, others) is None
    for tables in ({("_meta", "run_records")}, {("_meta", "run_records_v2")}, BOTH):
        request = model_history_request(1, tables)
        assert request is not None
        assert ("UNION ALL" in request.sql) == (tables == BOTH)


def test_runs_without_any_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MODELS)
    assert command(capsys, "runs", str(tmp_path)) == (0, "scheduler: has not run\nno runs yet\n")


# --- with a lake (DuckLake extension, CI) --------------------------------------------------------


def failed_record() -> RunRecord:
    return RunRecord.model_validate(
        json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "contracts/run-record/v1/examples/valid/failed.json"
            ).read_text(encoding="utf-8")
        )
    )


@contextlib.contextmanager
def recorded_lake(folder: Path, record: RunRecord) -> Iterator[Lake]:
    """A lake of its own, opened through the store's public API, holding `record`: the store
    binds every parameter of a statement or refuses it (#246 item 6)."""
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(folder))
    lake = open_lake(location, lake_open._limits())
    try:
        with lake.transaction() as transaction:
            for statement in record_statements([record]):
                transaction.execute(statement)
        yield lake
    finally:
        lake.close()


def lake_read(lake: Lake, request: ReadRequest) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = lake.read(request).read_all().to_pylist()
    return rows


def test_record_statements_bind_every_value(tmp_path: Path) -> None:
    record = failed_record()
    statements = record_statements([record])
    insert = statements[-1]
    assert insert.sql.startswith("INSERT INTO _meta.run_records_v2 VALUES")
    # Both tables exist once any record does; the old one keeps its own snapshot column (#394).
    creates = [statement.sql for statement in statements[:-1]]
    assert "CREATE TABLE IF NOT EXISTS _meta.run_records (" in creates[1]
    assert "snapshot_id BIGINT" in creates[1]
    assert "CREATE TABLE IF NOT EXISTS _meta.run_records_v2 (" in creates[2]
    assert "snapshot_id" not in creates[2]
    assert "Conversion" not in insert.sql  # values never enter the SQL text
    values = {parameter.name: parameter.value for parameter in insert.parameters}
    assert values["p_error_code"] == "STORE_QUERY_FAILED"
    assert values["p_rows_written"] is None
    assert values["p_project_dirty"] == "false"
    assert "p_snapshot_id" not in values  # resolved from the row's own snapshot when read
    # Each parameter is written exactly once in the statement.
    for parameter in insert.parameters:
        assert len(re.findall(rf"\${parameter.name}\b", insert.sql)) == 1, parameter.name
    # Every parameter is found and bound, with its declared type, the way the store binds it.
    with recorded_lake(tmp_path, record) as lake:
        sql = "SELECT error_code, rows_written, project_dirty FROM _meta.run_records_v2"
        request = ReadRequest(
            kind="read",
            sql=sql,
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=10,
        )
        assert lake_read(lake, request) == [
            {"error_code": "STORE_QUERY_FAILED", "rows_written": None, "project_dirty": False}
        ]


def test_history_parameters_bind(tmp_path: Path) -> None:
    record = failed_record()
    with recorded_lake(tmp_path, record) as lake:
        tables = lake.tables()
        assert set(tables) >= BOTH
        request = history_request(lake.current_snapshot().id, "America/La_Paz", 5, tables)
        assert request is not None
        assert len(request.parameters) == 2
        for parameter in request.parameters:
            assert len(re.findall(rf"\${parameter.name}\b", request.sql)) == 1, parameter.name
        assert [row["run_id"] for row in lake_read(lake, request)] == [record.run_id]


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
    # #394 criterion 3: `runs`, `run` and the target's new rows name the same snapshot.
    (line,) = runs_lines(capsys, tmp_path)
    assert runs_snapshot(line) == snapshot.group(1)
    assert lake_rows(tmp_path, "SELECT DISTINCT snapshot_id FROM lake.s.a") == [
        (int(snapshot.group(1)),)
    ]
    # Criterion 10: the resolved snapshot is BIGINT, rows_written stays HUGEINT.
    assert lake_rows(
        tmp_path,
        f"SELECT DISTINCT typeof({RESOLVED}), typeof(rows_written) FROM lake._meta.run_records_v2",
    ) == [("BIGINT", "HUGEINT")]
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
    tables = lake_rows(
        tmp_path,
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 's'",
    )
    assert tables == [(0,)]
    # Nothing else committed: the frozen table gets no row (#394).
    assert lake_rows(tmp_path, "SELECT count(*) FROM lake._meta.run_records") == [(0,)]


def test_runs_lists_newest_first_in_the_project_time_zone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MODELS)
    first = command(capsys, "run", str(tmp_path))[1]
    second = command(capsys, "run", str(tmp_path))[1]
    ids = [re.search(r"^run (\S+)$", out, re.MULTILINE).group(1) for out in (first, second)]  # type: ignore[union-attr]
    code, out = command(capsys, "runs", str(tmp_path))
    assert code == 0
    # The first line is the scheduler's state (#69); the runs follow.
    assert out.splitlines()[0] == "scheduler: has not run"
    lines = out.splitlines()[1:]
    assert [line.split()[0] for line in lines] == [ids[1], ids[0]]
    assert "success" in lines[0]
    assert "s.a, s.b, s.c" in lines[0]
    started_us = min(int(str(r["started_us"])) for r in records(tmp_path) if r["run_id"] == ids[1])
    local = dt.datetime.fromtimestamp(started_us / 1_000_000, zoneinfo.ZoneInfo("America/La_Paz"))
    assert local.strftime("%Y-%m-%d %H:%M:%S") in lines[0]
    code, out = command(capsys, "runs", str(tmp_path), "--limit", "1")
    assert out.splitlines()[1].split()[0] == ids[1]
    assert len(out.splitlines()) == 2


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
            # A database source is reached only by the models that read it, in a worker of its
            # own (#372): this one fails the run.
            "models/staging/billed.sql": "SELECT * FROM production.bills",
        },
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert secret not in out
    rows = records(tmp_path)
    assert rows
    assert {r["outcome"] for r in rows} == {"failed"}
    failed = {r["model"]: r["error_code"] for r in rows if r["error_code"]}
    assert failed == {"staging.billed": "STORE_CONNECTION_FAILED"}
    assert secret not in json.dumps(rows, default=str)


# --- failure paths, with a stand-in lake ---------------------------------------------------------


class FakeTransaction:
    def __init__(self) -> None:
        self.result: object = None
        self.executed: list[object] = []

    def execute(self, request: object) -> None:
        self.executed.append(request)

    def value(self, request: object) -> str:
        return "5"


class FakeLake:
    """Fails where a test asks: when a transaction starts or commits. Only what a transaction
    that did not fail executed is kept, as a lake keeps only what commits."""

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
        transaction = FakeTransaction()
        yield transaction
        if first and self.fail == "commit":
            raise StoreError("STORE_TRANSACTION_CONFLICT", "Another writer changed it.", {})
        self.executed.extend(transaction.executed)

    def tables(self) -> set[tuple[str, str]]:
        return set()  # no leftovers (#88), and no record committed

    def close(self) -> None:
        pass


def recorded(lake: FakeLake) -> list[dict[str, str | None]]:
    rows = []
    for request in lake.executed:
        sql = getattr(request, "sql", "")
        if sql.startswith("INSERT INTO _meta.run_records_v2"):
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
    monkeypatch.setattr(lake_open, "open_lake", lambda *args, **kwargs: lake)
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == 1
    assert code in out
    rows = recorded(lake)
    assert [(r["p_model"], r["p_outcome"], r["p_error_code"]) for r in rows] == [
        ("s.a", "failed", code),
        ("s.b", "failed", code),
        ("s.c", "failed", code),
    ]


def test_runs_limit_must_be_positive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MODELS)
    assert main(["runs", str(tmp_path), "--limit", "0"]) == 2


def test_the_record_names_the_run_s_own_snapshot_when_another_writer_commits_right_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #255: two clones sharing one lake; the other commits right after this run's commit. The
    # records name the run's snapshot, and rows are counted on it.
    project(tmp_path, MODELS)
    real = store_transaction._commit
    injected: list[int] = []

    def commit_then_another_writer(cursor: duckdb.DuckDBPyConnection) -> None:
        real(cursor)
        if not injected:
            injected.append(1)
            cursor.execute("CREATE TABLE lake.main.other_clone AS SELECT 1 AS x")  # own commit

    monkeypatch.setattr(store_transaction, "_commit", commit_then_another_writer)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    recorded = printed_snapshot(out)
    rows = [r for r in records(tmp_path) if r["outcome"] == "success"]
    assert {r["snapshot_id"] for r in rows} == {recorded}
    assert {r["rows_written"] for r in rows if r["model"] == "s.a"} == {"5"}
    changes: dict[object, Any] = dict(
        lake_rows(tmp_path, "SELECT snapshot_id, changes FROM ducklake_snapshots('lake')")  # type: ignore[arg-type]
    )
    # The run's commit, with its records; DuckLake lists the tables in creation order, which
    # models of one level need not keep (the suite runs in a random order on 3.15).
    created = changes[recorded]["tables_created"]
    assert sorted(created) == ["_meta.run_records", "_meta.run_records_v2", "s.a", "s.b", "s.c"]
    assert changes[recorded + 1]["tables_created"] == ["main.other_clone"]  # the other writer


# --- #394: the records commit with the data ------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import os, sys
    import orvex.store.lake as store_lake
    import orvex.store.transaction as store_transaction
    from orvex.server import run as run_command
    from orvex.server.cli import main

    folder, mode, token_file = sys.argv[1], sys.argv[2], sys.argv[3]
    armed = []
    records = run_command.record_statements

    def arming(items):
        armed.append(True)
        return records(items)

    committed = store_lake.Lake._committed

    def killed(self, token):
        if armed:  # the run's own commit just happened: the process ends here
            with open(token_file, "w", encoding="utf-8") as handle:
                handle.write(token)
            os._exit(3)
        return committed(self, token)

    commit = store_transaction._commit

    def another_writer_first(cursor):
        if len(armed) == 1:  # the run's block ran: another writer commits the same table
            other = cursor.cursor()
            other.execute("DROP TABLE lake.s.a")
            other.close()
        commit(cursor)

    run_command.record_statements = arming
    if mode == "kill":
        store_lake.Lake._committed = killed
    else:
        store_transaction._commit = another_writer_first
    code = main(["run", folder])
    with open(token_file, "w", encoding="utf-8") as handle:
        handle.write(f"{code} {len(armed)}")
    """
)


def child_run(folder: Path, mode: str, result: Path) -> subprocess.CompletedProcess[str]:
    """`run` in a process of its own, stopped or disturbed as `mode` says. A process of its own:
    DuckDB 2.0.0 keeps a local catalog attached in the process after a commit conflict."""
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(folder), mode, str(result)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )


def test_a_kill_right_after_the_commit_leaves_data_and_records(tmp_path: Path) -> None:
    # Criterion 1: the process ends at the first step after the real commit (the token lookup).
    folder = project(tmp_path / "p", MODELS)
    token_file = tmp_path / "token"
    child = child_run(folder, "kill", token_file)
    assert child.returncode == 3, child.stderr
    committed = snapshot_of_token(folder, token_file.read_text(encoding="utf-8"))
    assert lake_rows(folder, "SELECT count(*) FROM lake.s.a") == [(5,)]
    rows = records(folder)
    assert [(r["model"], r["outcome"]) for r in rows] == [
        ("s.a", "success"),
        ("s.b", "success"),
        ("s.c", "success"),
    ]
    assert {r["snapshot_id"] for r in rows} == {committed}


def test_ctrl_c_right_after_the_commit_adds_no_failed_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Criterion 1b: the run is committed and recorded; the interruption still goes on.
    project(tmp_path, MODELS)
    armed = armed_on_records(monkeypatch)
    tokens: list[str] = []
    real = Lake._committed

    def interrupted(self: Lake, token: str) -> object:
        if armed:
            tokens.append(token)
            raise KeyboardInterrupt
        return real(self, token)

    monkeypatch.setattr(Lake, "_committed", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["run", str(tmp_path)])
    out = capsys.readouterr().out
    run_id = re.search(r"^run (\S+)$", out, re.MULTILINE)
    assert run_id
    monkeypatch.undo()
    rows = records(tmp_path)
    assert [(r["run_id"], r["model"], r["outcome"]) for r in rows] == [
        (run_id.group(1), "s.a", "success"),
        (run_id.group(1), "s.b", "success"),
        (run_id.group(1), "s.c", "success"),
    ]
    assert {r["snapshot_id"] for r in rows} == {snapshot_of_token(tmp_path, tokens[0])}


def test_ctrl_c_before_the_commit_commits_nothing_and_records_the_run_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Criterion 2.
    project(tmp_path, MODELS)
    armed = armed_on_records(monkeypatch)
    real = store_transaction._commit

    def interrupted(cursor: duckdb.DuckDBPyConnection) -> None:
        if armed and len(armed) == 1:
            raise KeyboardInterrupt
        real(cursor)

    monkeypatch.setattr(store_transaction, "_commit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["run", str(tmp_path)])
    capsys.readouterr()
    assert [(r["outcome"], r["error_code"], r["snapshot_id"]) for r in records(tmp_path)] == [
        ("failed", "STORE_CANCELLED", None)
    ] * 3
    tables = (
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 's'"
    )
    assert lake_rows(tmp_path, tables) == [(0,)]


INCREMENTAL = {
    "models/s/m.sql": "SELECT id, read_at FROM feed.data WHERE read_at > $since",
    "models/s/m.yaml": (
        "version: 1\nmaterialized: incremental\n"
        "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
    ),
    "models/s/t.sql": "SELECT id FROM s.m",
    "models/s/v.sql": "SELECT count(*) AS n FROM s.m",
    "models/s/v.yaml": "version: 1\nmaterialized: view\n",
}


def incremental_project(folder: Path) -> Path:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        "COPY (SELECT range AS id, TIMESTAMPTZ '2024-01-01 00:00:00+00' + to_hours(range) "
        f"AS read_at FROM range(4)) TO '{folder / 'data' / 'f.parquet'}'"
    )
    connections = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/f.parquet\n"
    return write(folder, {"project.yaml": PROJECT, "connections.yaml": connections, **INCREMENTAL})


def test_a_run_that_changes_no_data_shows_the_snapshot_holding_its_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Criterion 4 (Owner decision on #394): one new snapshot, the records' own; every table
    # outside `_meta` reads as at the snapshot the run started from.
    incremental_project(tmp_path)
    assert command(capsys, "run", str(tmp_path))[0] == 0
    write(tmp_path, {"models/s/t.yaml": "version: 1\nmaterialized: view\n"})  # nothing stored
    started = latest_snapshot(tmp_path)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 0 new rows (4 in table)" in out
    snapshot = printed_snapshot(out)
    assert snapshot > started
    assert runs_snapshot(runs_lines(capsys, tmp_path)[0]) == str(snapshot)
    run_id = re.search(r"^run (\S+)$", out, re.MULTILINE)
    assert run_id
    rows = [r for r in records(tmp_path) if r["run_id"] == run_id.group(1)]
    assert [(r["model"], r["outcome"], r["snapshot_id"]) for r in rows] == [
        ("s.m", "success", snapshot)
    ]
    outside = (
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = 'lake' AND schema_name <> '_meta' ORDER BY ALL"
    )
    tables = lake_rows(tmp_path, outside)
    assert tables == [("s", "m"), ("s", "t")]  # s.t is a leftover now, kept as it was
    for schema, table in tables:
        at_run, at_start = (
            f"SELECT * FROM lake.{schema}.{table} AT (VERSION => {version})"
            for version in (snapshot, started)
        )
        differences = lake_rows(
            tmp_path,
            f"SELECT count(*) FROM (({at_run} EXCEPT ALL {at_start}) "
            f"UNION ALL ({at_start} EXCEPT ALL {at_run}))",
        )
        assert differences == [(0,)], (schema, table)


LEGACY_DDL = (
    "CREATE TABLE lake._meta.run_records (run_id VARCHAR, model VARCHAR, trigger VARCHAR, "
    "materialized VARCHAR, project_commit VARCHAR, project_dirty BOOLEAN, versions VARCHAR, "
    "since VARCHAR, rows_read HUGEINT, rows_written HUGEINT, watermark VARCHAR, tests VARCHAR, "
    "reconciliation VARCHAR, snapshot_id BIGINT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, "
    "outcome VARCHAR, error_code VARCHAR, error_message VARCHAR)"
)


def legacy_row(run_id: str, trigger: str, snapshot: str, day: int, outcome: str) -> str:
    error = "'STORE_QUERY_FAILED', 'failed'" if outcome == "failed" else "NULL, NULL"
    return (
        f"('{run_id}', 's.a', '{trigger}', 'table', NULL, NULL, '{{}}', NULL, NULL, "
        f"{'5' if outcome == 'success' else 'NULL'}, NULL, '[]', '[]', {snapshot}, "
        f"TIMESTAMPTZ '2024-01-0{day} 10:00:00+00', TIMESTAMPTZ '2024-01-0{day} 10:00:05+00', "
        f"'{outcome}', {error})"
    )


def history(folder: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    with open_project_lake(folder) as lake:
        tables = lake.tables()
        snapshot = lake.current_snapshot().id
        histories, last = (
            model_history_request(snapshot, tables),
            last_run_request(snapshot, tables),
        )
        assert histories is not None
        assert last is not None
        rows = sorted(lake.read(histories).read_all().to_pylist(), key=lambda row: row["model"])
        (latest,) = lake.read(last).read_all().to_pylist()
    return rows, latest


def test_records_of_the_earlier_layout_read_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Criterion 5: a lake whose records are all in `_meta.run_records`, written before #394.
    project(tmp_path, MODELS)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    first = printed_snapshot(out)
    lake_rows(tmp_path, "DROP TABLE lake._meta.run_records_v2", writable=True)
    lake_rows(tmp_path, "DROP TABLE lake._meta.run_records", writable=True)
    lake_rows(tmp_path, LEGACY_DDL, writable=True)
    rows = ", ".join(
        [
            legacy_row("legacy-1", "full_refresh", str(first), 1, "success"),
            legacy_row("legacy-2", "manual", "NULL", 2, "failed"),
            legacy_row("legacy-3", "manual", "NULL", 3, "success"),  # "after the commit" warning
        ]
    )
    lake_rows(tmp_path, f"INSERT INTO lake._meta.run_records VALUES {rows}", writable=True)
    # What the readers returned before #394, for these rows (America/La_Paz is UTC-4).
    assert runs_lines(capsys, tmp_path) == [
        "legacy-3  2024-01-03 06:00:00  success  snapshot -  s.a",
        "legacy-2  2024-01-02 06:00:00  failed   snapshot -  s.a",
        f"legacy-1  2024-01-01 06:00:00  success  snapshot {first}  s.a",
    ]
    assert history(tmp_path) == (
        [
            {
                "model": "s.a",
                "success_start": "2024-01-03T10:00:00.000000+00:00",
                "success_end": "2024-01-03T10:00:05.000000+00:00",
                "full_refresh": "2024-01-01T10:00:00.000000+00:00",
            }
        ],
        {"started": "2024-01-03T10:00:00.000000+00:00", "failed": None},
    )
    # New records join them, ordered by start across both tables.
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    run_id = re.search(r"^run (\S+)$", out, re.MULTILINE)
    assert run_id
    lines = runs_lines(capsys, tmp_path)
    assert [line.split()[0] for line in lines] == [
        run_id.group(1),
        "legacy-3",
        "legacy-2",
        "legacy-1",
    ]
    assert runs_snapshot(lines[0]) == str(printed_snapshot(out))
    assert runs_snapshot(lines[3]) == str(first)
    histories, latest = history(tmp_path)
    assert [row["model"] for row in histories] == ["s.a", "s.b", "s.c"]
    assert histories[0]["full_refresh"] == "2024-01-01T10:00:00.000000+00:00"
    assert str(histories[0]["success_start"]) > "2025"
    assert latest["started"] == histories[0]["success_start"]
    assert lake_rows(tmp_path, "SELECT count(*) FROM lake._meta.run_records") == [(3,)]  # frozen


def test_maintenance_keeps_every_record_s_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Criterion 6: compaction and the expiry of every snapshot but the current one.
    project(tmp_path, MODELS)
    write(tmp_path, {"project.yaml": PROJECT + "retention:\n  snapshots: 1d\n"})
    printed = []
    for _ in range(3):
        code, out = command(capsys, "run", str(tmp_path))
        assert code == 0, out
        printed.append(printed_snapshot(out))
    before = [(r["run_id"], r["model"], r["snapshot_id"]) for r in records(tmp_path)]
    assert sorted({snapshot for _, _, snapshot in before}) == printed  # type: ignore[type-var]
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    text = io.StringIO()
    assert maintenance.maintain(tmp_path, load_identity(), text, clock=lambda: moment) == 0
    ((oldest,),) = lake_rows(tmp_path, "SELECT min(snapshot_id) FROM ducklake_snapshots('lake')")
    assert int(str(oldest)) > max(printed), text.getvalue()  # every run's snapshot expired
    assert [(r["run_id"], r["model"], r["snapshot_id"]) for r in records(tmp_path)] == before


def test_another_writer_first_fails_the_run_and_records_it_after_the_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # R1: another writer of the same catalog commits the same table between the run's block
    # and its commit (a connection of its own: a local catalog is attached once per process).
    folder = project(tmp_path / "p", MODELS)
    assert command(capsys, "run", str(folder))[0] == 0
    result = tmp_path / "result"
    child = child_run(folder, "conflict", result)
    assert "STORE_TRANSACTION_CONFLICT" in child.stdout, child.stdout + child.stderr
    # Exit 1; the failed records came after the rollback, in a transaction of their own.
    assert result.read_text(encoding="utf-8") == "1 2"
    last = records(folder)[3:]
    assert [(r["outcome"], r["error_code"]) for r in last] == [
        ("failed", "STORE_TRANSACTION_CONFLICT")
    ] * 3
    # The other writer's commit stands: s.a is gone, and the run's own s.a was not committed.
    tables = (
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 's'"
    )
    assert sorted(lake_rows(folder, tables)) == [("b",), ("c",)]


def timed_out(transaction: Transaction) -> StoreError:
    """What the store's guard leaves when the transaction's time limit expires mid-statement."""
    return transaction._interrupted(
        StoreError("STORE_TIMEOUT", "The transaction exceeded its time limit.", {})
    )


@pytest.mark.parametrize("where", ["count", "records"])
def test_the_time_limit_expiring_while_counting_or_recording_rolls_the_run_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    # R2.
    project(tmp_path, MODELS)
    if where == "count":

        def counting(transaction: Transaction, model: object) -> str:
            raise timed_out(transaction)

        monkeypatch.setattr(run_command, "_count", counting)
    else:
        real = Transaction.execute

        def recording(self: Transaction, request: StatementRequest) -> None:
            if request.sql.startswith("INSERT INTO _meta.run_records_v2") and not stopped:
                stopped.append(True)
                raise timed_out(self)
            real(self, request)

        stopped: list[bool] = []
        monkeypatch.setattr(Transaction, "execute", recording)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1, out
    assert [(r["outcome"], r["error_code"]) for r in records(tmp_path)] == [
        ("failed", "STORE_TIMEOUT")
    ] * 3
    tables = (
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' AND schema_name = 's'"
    )
    assert lake_rows(tmp_path, tables) == [(0,)]


def test_a_cancel_while_recording_rolls_the_run_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3: the scheduler's stop, say, arrives as the records are inserted.
    project(tmp_path, MODELS)
    real = Transaction.execute
    cancelled: list[bool] = []

    def recording(self: Transaction, request: StatementRequest) -> None:
        if request.sql.startswith("INSERT INTO _meta.run_records_v2") and not cancelled:
            cancelled.append(True)
            self.cancel()
        real(self, request)

    monkeypatch.setattr(Transaction, "execute", recording)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1, out
    assert [(r["outcome"], r["error_code"]) for r in records(tmp_path)] == [
        ("failed", "STORE_CANCELLED")
    ] * 3


def test_a_failed_run_whose_records_cannot_be_written_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # R4.
    project(tmp_path, {**MODELS, "models/s/b.sql": "SELECT CAST('x' AS INTEGER) AS n FROM s.a"})

    def broken(*_: object) -> list[StatementRequest]:
        return [
            StatementRequest(kind="statement", sql="INSERT INTO nowhere VALUES (1)", parameters=[])
        ]

    monkeypatch.setattr(run_command, "record_statements", broken)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    lines = out.splitlines()
    run_problem = next(
        i for i, line in enumerate(lines) if line.startswith("STORE_QUERY_FAILED models/s/b.sql")
    )
    store_problem = next(
        i for i, line in enumerate(lines) if "run records were not written" in line
    )
    assert run_problem < store_problem
