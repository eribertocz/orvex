"""P1-16: reconciliation against the source, inside the run transaction."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MONTHLY = "measure: sum(kwh), group_by: [\"date_trunc('month', read_at)\"], source: feed.data"


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def project(folder: Path, model: str, checks: str, extra: dict[str, str] | None = None) -> None:
    """Readings over three months (one without a date), `DECIMAL(18,4)`."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    rows = (
        "('2024-01-05 10:00:00+00', '1.0000'), ('2024-01-20 10:00:00+00', '2.0001'), "
        "('2024-02-01 10:00:00+00', '3.0000'), (NULL, '4.0000'), "
        "('2024-03-01 10:00:00+00', '5.0000')"
    )
    duckdb.connect().execute(
        "COPY (SELECT read_at::TIMESTAMPTZ AS read_at, kwh::DECIMAL(18,4) AS kwh "
        f"FROM (VALUES {rows}) AS t(read_at, kwh)) TO '{folder / 'data' / 'feed.parquet'}'"
    )
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": model,
            "models/s/m.yaml": f"version: 1\nreconciliation:\n{checks}",
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


def results(folder: Path) -> list[dict[str, object]]:
    (row,) = lake_value(
        folder,
        "SELECT reconciliation FROM lake._meta.run_records WHERE model = 's.m' "
        "ORDER BY started_at DESC LIMIT 1",
    )
    found: list[dict[str, object]] = json.loads(str(row[0]))
    return found


COPY = "SELECT * FROM feed.data"
BOTH = (
    f"  - {{name: monthly, {MONTHLY}}}\n  - {{name: rows, measure: count(*), source: feed.data}}\n"
)
# January off by 0.0001.
OFF = "SELECT read_at, CASE WHEN kwh = 2.0001 THEN 2.0000 ELSE kwh END AS kwh FROM feed.data"


def test_a_faithful_model_reconciles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(
        tmp_path,
        COPY,
        BOTH,
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert results(tmp_path) == [
        {"name": "monthly", "outcome": "passed", "failing_rows": "0"},
        {"name": "rows", "outcome": "passed", "failing_rows": "0"},
    ]


def test_a_divergence_rolls_the_run_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, COPY, f"  - {{name: monthly, {MONTHLY}}}\n")
    assert command(capsys, "run", str(tmp_path))[0] == 0
    (before,) = lake_value(tmp_path, "SELECT count(*) FROM ducklake_snapshots('lake')")
    write(tmp_path, {"models/s/m.sql": OFF})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "RECONCILIATION_FAILED models/s/m.yaml monthly failed on 1 groups" in out
    assert results(tmp_path) == [{"name": "monthly", "outcome": "failed", "failing_rows": "1"}]
    # Only the run records commit; the table keeps the faithful copy.
    assert lake_value(tmp_path, "SELECT count(*) FROM ducklake_snapshots('lake')") == [
        (int(str(before[0])) + 1,)
    ]
    assert lake_value(tmp_path, "SELECT count(*) FROM lake.s.m WHERE kwh = 2.0001") == [(1,)]


@pytest.mark.parametrize(("tolerance", "code"), [("0.0001", 0), ("0.00009", 1)])
def test_the_tolerance_is_exact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tolerance: str, code: int
) -> None:
    project(tmp_path, OFF, f"  - {{name: monthly, {MONTHLY}, tolerance: {tolerance}}}\n")
    exit_code, out = command(capsys, "run", str(tmp_path))
    assert exit_code == code, out


def test_missing_groups_fail_and_null_groups_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(
        tmp_path,
        "SELECT * FROM feed.data WHERE read_at IS NULL OR read_at < '2024-03-01'",
        BOTH,
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    # March is missing (1 group); the NULL month matches; the total differs (1 group).
    assert results(tmp_path) == [
        {"name": "monthly", "outcome": "failed", "failing_rows": "1"},
        {"name": "rows", "outcome": "failed", "failing_rows": "1"},
    ]
    assert "monthly failed on 1 groups" in out
    assert "rows failed on 1 groups" in out


def test_one_failing_check_of_two_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, OFF, BOTH, {"models/t/after.sql": "SELECT count(*) AS n FROM s.m"})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert results(tmp_path) == [
        {"name": "monthly", "outcome": "failed", "failing_rows": "1"},
        {"name": "rows", "outcome": "passed", "failing_rows": "0"},
    ]
    assert out.count("RECONCILIATION_FAILED") == 1
    assert lake_value(
        tmp_path, "SELECT outcome FROM lake._meta.run_records WHERE model = 't.after'"
    ) == [("not_run",)]


@pytest.mark.parametrize(
    ("checks", "code", "where"),
    [
        (
            "  - {name: a, measure: 'sum(kwh)) FROM x --', source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0/measure",
        ),
        (
            "  - {name: a, measure: '(SELECT 1)', source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0/measure",
        ),
        (
            "  - {name: a, measure: 'CAST(avg(kwh) AS DOUBLE)', source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0/measure",
        ),
        (
            "  - {name: a, measure: count(*), source: nowhere.t}\n",
            "MODEL_REFERENCE_UNKNOWN",
            "/reconciliation/0/source",
        ),
        (
            "  - {name: a, measure: count(*), group_by: [nothing], source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0",
        ),
        (
            "  - {name: a, measure: kwh, source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0",
        ),
        (
            "  - {name: a, measure: 'max(read_at)', source: feed.data}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0/measure",
        ),
        (
            "  - {name: a, measure: count(*), source: feed.data, tolerance: -1}\n",
            "MODEL_CONFIG_INVALID",
            "/reconciliation/0/tolerance",
        ),
    ],
)
def test_invalid_checks_are_refused_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], checks: str, code: str, where: str
) -> None:
    project(tmp_path, COPY, checks)
    exit_code, out = command(capsys, "validate", str(tmp_path))
    assert exit_code == 1
    assert out.startswith(f"{code} models/s/m.yaml"), out
    assert where in out
    assert not (tmp_path / f".{SLUG}").exists()
