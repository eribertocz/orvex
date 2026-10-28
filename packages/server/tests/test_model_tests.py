"""P1-15: model tests run inside the run transaction (write, audit, publish)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = (
    "version: 1\nconnections:\n"
    "  feed:\n    type: parquet\n    path: data/feed.parquet\n"
    "  registry:\n    type: parquet\n    path: data/consumers.parquet\n"
)
READINGS = "SELECT * FROM feed.data"
CONSUMERS = "SELECT * FROM registry.data"


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, rows: str) -> None:
    """Readings: reading_id, consumer_id, kwh DECIMAL(18,4), read_at TIMESTAMPTZ, status."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        "COPY (SELECT reading_id::BIGINT AS reading_id, consumer_id::VARCHAR AS consumer_id, "
        "kwh::DECIMAL(18,4) AS kwh, read_at::TIMESTAMPTZ AS read_at, status::VARCHAR AS status "
        f"FROM (VALUES {rows}) AS t(reading_id, consumer_id, kwh, read_at, status)) "
        f"TO '{folder / 'data' / 'feed.parquet'}'"
    )
    connection.execute(
        "COPY (SELECT * FROM (VALUES ('C1'), ('C2')) AS t(consumer_id)) "
        f"TO '{folder / 'data' / 'consumers.parquet'}'"
    )


GOOD = (
    "(1, 'C1', '1.0000', '2024-01-01 10:00:00+00', 'valid'), "
    "(2, 'C2', '0', '2024-01-02 10:00:00+00', 'estimated'), "
    "(3, 'C1', '99999999999999.9999', '2024-01-03 10:00:00+00', 'valid')"
)


def project(
    folder: Path, tests: str, rows: str = GOOD, extra: dict[str, str] | None = None
) -> None:
    feed(folder, rows)
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            # `a.readings` sorts before `z.consumers`: only the relationship test orders them.
            "models/a/readings.sql": READINGS,
            "models/a/readings.yaml": f"version: 1\ntests:\n{tests}",
            "models/z/consumers.sql": CONSUMERS,
            **(extra or {}),
        },
    )


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def lake_value(folder: Path, sql: str) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    result = lake.execute(sql).fetchall()
    lake.close()
    return result


def results(folder: Path, model: str = "a.readings") -> list[dict[str, object]]:
    (row,) = lake_value(
        folder,
        f"SELECT tests FROM lake._meta.run_records WHERE model = '{model}' "
        "ORDER BY started_at DESC LIMIT 1",
    )
    found: list[dict[str, object]] = json.loads(str(row[0]))
    return found


def snapshots(folder: Path) -> int:
    (row,) = lake_value(folder, "SELECT count(*) FROM ducklake_snapshots('lake')")
    return int(str(row[0]))


ALL = (
    "  - unique: [reading_id]\n"
    "  - not_null: [consumer_id, read_at]\n"
    "  - accepted_range: {column: kwh, min: 0, max: 99999999999999.9999}\n"
    "  - accepted_values: {column: status, values: [valid, estimated]}\n"
    "  - relationship: {column: consumer_id, to: z.consumers, field: consumer_id}\n"
)


def test_passing_tests_publish_and_are_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, ALL)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert results(tmp_path) == [
        {"name": "unique(reading_id)", "outcome": "passed", "failing_rows": "0"},
        {"name": "not_null(consumer_id, read_at)", "outcome": "passed", "failing_rows": "0"},
        {"name": "accepted_range(kwh)", "outcome": "passed", "failing_rows": "0"},
        {"name": "accepted_values(status)", "outcome": "passed", "failing_rows": "0"},
        {
            "name": "relationship(consumer_id → z.consumers.consumer_id)",
            "outcome": "passed",
            "failing_rows": "0",
        },
    ]
    # The related model ran first although the SQL never names it.
    assert out.index("built z.consumers") < out.index("built a.readings")


BAD = (
    "(1, 'C1', '-0.0001', '2024-01-01 10:00:00+00', 'valid'), "
    "(1, NULL, '0', '2024-01-02 10:00:00+00', 'estimated'), "
    "(2, NULL, '0.1', NULL, 'voided'), "
    "(3, 'C9', '99999999999999.9999', '2024-01-03 10:00:00+00', NULL), "
    "(4, NULL, '5', '2024-01-04 10:00:00+00', 'valid')"
)


def test_failing_tests_roll_the_whole_run_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, ALL)
    assert command(capsys, "run", str(tmp_path))[0] == 0
    before = snapshots(tmp_path)
    table = lake_value(
        tmp_path, "SELECT CAST(COLUMNS(*) AS VARCHAR) FROM lake.a.readings ORDER BY ALL"
    )
    feed(tmp_path, BAD)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    failing = {r["name"]: r["failing_rows"] for r in results(tmp_path) if r["outcome"] == "failed"}
    assert failing == {
        "unique(reading_id)": "2",
        "not_null(consumer_id, read_at)": "3",
        "accepted_range(kwh)": "1",
        "accepted_values(status)": "1",
        "relationship(consumer_id → z.consumers.consumer_id)": "1",
    }
    # Every failing test is printed; the data stays as it was (only the run records commit).
    lines = [line for line in out.splitlines() if line.startswith("MODEL_TEST_FAILED")]
    assert (
        lines[0] == "MODEL_TEST_FAILED models/a/readings.yaml unique(reading_id) failed on 2 rows"
    )
    assert len(lines) == 5
    assert snapshots(tmp_path) == before + 1
    assert (
        lake_value(tmp_path, "SELECT CAST(COLUMNS(*) AS VARCHAR) FROM lake.a.readings ORDER BY ALL")
        == table
    )


def test_later_models_do_not_run_after_a_failed_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(
        tmp_path,
        "  - unique: [reading_id]\n  - accepted_values: {column: status, values: [valid]}\n",
        BAD,
        {"models/b/after.sql": "SELECT count(*) AS n FROM a.readings"},
    )
    code, _out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert [r["outcome"] for r in results(tmp_path)] == ["failed", "failed"]
    (outcome,) = lake_value(
        tmp_path, "SELECT outcome FROM lake._meta.run_records WHERE model = 'b.after'"
    )
    assert outcome == ("not_run",)


def test_bounds_are_exact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = (
        "(1, 'C1', '0.1', '2024-01-01 00:00:00+00', 'valid'), "
        "(2, 'C2', '0.1000', '2024-01-01 00:00:00.000001+00', 'valid')"
    )
    tests = (
        "  - accepted_range: {column: kwh, min: 0.1}\n"  # a float 0.1 would be above 0.1000
        "  - accepted_range: {column: read_at, min: '2024-01-01 00:00:00+00'}\n"
        "  - accepted_range: {column: kwh, max: 0.09999}\n"
    )
    project(tmp_path, tests, rows)
    code, _out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert [(r["outcome"], r["failing_rows"]) for r in results(tmp_path)] == [
        ("passed", "0"),
        ("passed", "0"),
        ("failed", "2"),
    ]


def test_a_failed_test_removes_the_appended_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sidecar = (
        "version: 1\nmaterialized: incremental\n"
        "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
        "tests:\n  - unique: [reading_id]\n"
    )
    project(
        tmp_path,
        "  - unique: [reading_id]\n",
        extra={
            "models/a/readings.sql": "SELECT * FROM feed.data WHERE read_at > $since",
            "models/a/readings.yaml": sidecar,
        },
    )
    assert command(capsys, "run", str(tmp_path))[0] == 0
    # A newer row repeating reading_id 1: appended, then caught by the test, then rolled back.
    feed(tmp_path, "(1, 'C1', '1.0000', '2024-02-01 10:00:00+00', 'valid')")
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "unique(reading_id) failed on 2 rows" in out
    assert lake_value(tmp_path, "SELECT count(*) FROM lake.a.readings") == [(3,)]
    assert lake_value(tmp_path, "SELECT watermark FROM lake._meta.watermarks") == [
        ("2024-01-03T10:00:00.000000+00:00",)
    ]


def test_tests_on_a_view_run_on_its_sql(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(
        tmp_path,
        "  - unique: [reading_id]\n",
        extra={
            "models/a/recent.sql": "SELECT * FROM a.readings WHERE reading_id > 1",
            "models/a/recent.yaml": "version: 1\nmaterialized: view\ntests:\n  - not_null: [kwh]\n",
        },
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "tested a.recent" in out
    assert results(tmp_path, "a.recent") == [
        {"name": "not_null(kwh)", "outcome": "passed", "failing_rows": "0"}
    ]


@pytest.mark.parametrize(
    ("tests", "code", "path"),
    [
        ("  - unique: [nothing]\n", "MODEL_CONFIG_INVALID", "/tests/0/unique/0"),
        ("  - accepted_range: {column: status, min: 0}\n", "MODEL_CONFIG_INVALID", None),
        ("  - accepted_range: {column: kwh}\n", "MODEL_CONFIG_INVALID", None),
        ("  - accepted_values: {column: status, values: [1, 2]}\n", "MODEL_CONFIG_INVALID", None),
        (
            "  - relationship: {column: consumer_id, to: nowhere.t, field: consumer_id}\n",
            "MODEL_REFERENCE_UNKNOWN",
            None,
        ),
    ],
)
def test_invalid_tests_are_refused_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tests: str, code: str, path: str | None
) -> None:
    project(tmp_path, tests)
    exit_code, out = command(capsys, "validate", str(tmp_path))
    assert exit_code == 1
    assert out.startswith(f"{code} models/a/readings.yaml"), out
    if path is not None:
        assert path in out
    assert not (tmp_path / f".{SLUG}").exists()
