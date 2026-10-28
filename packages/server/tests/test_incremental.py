"""P1-11: incremental models with the append and merge strategies."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MODEL = "SELECT * FROM feed.data"


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, rows: str) -> None:
    """Replace the source rows: id, region, kwh DECIMAL(18,4), big HUGEINT, read_at TIMESTAMPTZ."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        "COPY (SELECT id::BIGINT AS id, region::VARCHAR AS region, kwh::DECIMAL(18,4) AS kwh, "
        "big::HUGEINT AS big, read_at::TIMESTAMPTZ AS read_at "
        f"FROM (VALUES {rows}) AS t(id, region, kwh, big, read_at)) "
        f"TO '{folder / 'data' / 'feed.parquet'}'"
    )


def project(folder: Path, sidecar: str, model: str = MODEL) -> None:
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": model,
            "models/s/m.yaml": sidecar,
        },
    )


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def table(folder: Path) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    rows = lake.execute(
        "SELECT id, region, CAST(kwh AS VARCHAR), CAST(big AS VARCHAR), "
        "CAST(epoch_us(read_at) AS VARCHAR) FROM lake.s.m ORDER BY id, region"
    ).fetchall()
    lake.close()
    return rows


APPEND = (
    "version: 1\nmaterialized: incremental\nincremental: {strategy: append, time_column: read_at}\n"
)
MERGE = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, time_column: read_at, unique_key: [id]}\n"
)
BIG = "170141183460469231731687303715884105727"


def rows(*items: tuple[int, str, str]) -> str:
    """Rows whose read_at is day id % 9 + 1 of January 2024, 10:00 UTC. Numbers are given as
    text so DuckDB converts them exactly (a bare 39-digit literal would be read as DOUBLE)."""
    return ", ".join(
        f"({i}, '{region}', '{kwh}', '{BIG}', '2024-01-0{i % 9 + 1} 10:00:00+00')"
        for i, region, kwh in items
    )


def test_append_adds_loaded_rows_and_keeps_them_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, rows((1, "n", "1.0001"), (2, "n", "2.0002"), (3, "s", "3.0003")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    feed(tmp_path, rows((4, "n", "4.0004"), (5, "s", "5.0005")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 2 rows" in out
    assert [row[0] for row in table(tmp_path)] == [1, 2, 3, 4, 5]
    # Exact values survive (DECIMAL, HUGEINT, TIMESTAMPTZ as text).
    assert table(tmp_path)[0][2:] == (
        "1.0001",
        BIG,
        "1704189600000000",
    )
    write(
        tmp_path, {"models/s/m.sql": "SELECT * REPLACE (CAST('x' AS INTEGER) AS id) FROM feed.data"}
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert len(table(tmp_path)) == 5


def test_merge_updates_matched_keys_and_inserts_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MERGE)
    feed(tmp_path, rows((1, "n", "1.0000"), (2, "n", "2.0000"), (3, "s", "3.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((2, "n", "2.5001"), (4, "s", "4.0000")))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 2 rows" in out
    assert [(row[0], row[2]) for row in table(tmp_path)] == [
        (1, "1.0000"),
        (2, "2.5001"),
        (3, "3.0000"),
        (4, "4.0000"),
    ]


def test_merge_with_a_composite_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MERGE.replace("unique_key: [id]", "unique_key: [id, region]"))
    feed(tmp_path, rows((1, "n", "1.0000"), (1, "s", "2.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, rows((1, "s", "9.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    assert [(row[0], row[1], row[2]) for row in table(tmp_path)] == [
        (1, "n", "1.0000"),
        (1, "s", "9.0000"),
    ]


@pytest.mark.parametrize(
    ("loaded", "code"),
    [
        ("(NULL, 'n', 1, 1, '2024-01-01 00:00:00+00')", "UNIQUE_KEY_NULL"),
        (
            "(7, 'n', 1, 1, '2024-01-01 00:00:00+00'), (7, 's', 2, 1, '2024-01-01 00:00:00+00')",
            "UNIQUE_KEY_DUPLICATED",
        ),
    ],
)
def test_merge_refuses_null_and_duplicated_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], loaded: str, code: str
) -> None:
    project(tmp_path, MERGE)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, loaded)
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == 1
    assert out.splitlines()[-1].startswith(f"{code} ")
    assert [row[0] for row in table(tmp_path)] == [1]


def test_schema_change_fails_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, rows((1, "n", "1.0000")))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    for changed in [
        "SELECT *, 1 AS extra FROM feed.data",
        "SELECT id, region AS area, kwh, big, read_at FROM feed.data",
        "SELECT id, region, CAST(kwh AS DOUBLE) AS kwh, big, read_at FROM feed.data",
    ]:
        write(tmp_path, {"models/s/m.sql": changed})
        code, out = command(capsys, "run", str(tmp_path))
        assert code == 1, changed
        assert "SCHEMA_CHANGED" in out
        assert len(table(tmp_path)) == 1


# --- validation ---------------------------------------------------------------------------------


def test_sidecar_rules_need_no_lake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, MERGE.replace(", unique_key: [id]", ""))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MISSING_UNIQUE_KEY models/s/m.yaml")
    project(tmp_path, APPEND.replace("time_column: read_at", "time_column: read_at, lookback: 1h"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert out.startswith("LOOKBACK_REQUIRES_UNIQUE_KEY models/s/m.yaml")
    assert not (tmp_path / f".{SLUG}").exists()


def test_column_rules_are_checked_by_binding_without_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feed(tmp_path, rows((1, "n", "1.0000")))
    project(tmp_path, APPEND.replace("time_column: read_at", "time_column: region"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("TIME_COLUMN_NOT_TEMPORAL models/s/m.yaml")
    project(tmp_path, MERGE.replace("unique_key: [id]", "unique_key: [nothing]"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("MODEL_CONFIG_INVALID models/s/m.yaml")
    assert not (tmp_path / f".{SLUG}").exists()
