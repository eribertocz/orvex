"""P0-09 acceptance tests on the `small` dataset. Every check is SQL over the Parquet files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tools.synthetic.generate import SyntheticError, generate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("small")
    generate("small", 42, out)
    return out


def scalar(dataset: Path, sql: str) -> object:
    connection = duckdb.connect()
    try:
        for table in ("consumers", "meters", "readings"):
            path = str(dataset / f"{table}.parquet").replace("'", "''")
            connection.execute(f"CREATE VIEW {table} AS FROM read_parquet('{path}')")
        row = connection.execute(sql).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def manifest(folder: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return loaded


def test_same_seed_is_byte_identical(dataset: Path, tmp_path: Path) -> None:
    generate("small", 42, tmp_path)
    assert manifest(tmp_path)["sha256"] == manifest(dataset)["sha256"]


def test_output_does_not_depend_on_thread_count(dataset: Path, tmp_path: Path) -> None:
    generate("small", 42, tmp_path, threads=1)
    assert manifest(tmp_path)["sha256"] == manifest(dataset)["sha256"]


def test_different_seed_changes_data_not_counts(dataset: Path, tmp_path: Path) -> None:
    generate("small", 7, tmp_path)
    assert manifest(tmp_path)["sha256"] != manifest(dataset)["sha256"]
    assert manifest(tmp_path)["rows"] == manifest(dataset)["rows"]


def test_small_counts_are_exact(dataset: Path) -> None:
    assert manifest(dataset)["rows"] == {"consumers": 10_000, "meters": 10_300, "readings": 240_720}
    assert manifest(dataset)["seed"] == 42
    assert manifest(dataset)["size"] == "small"


DISTINCT = "(SELECT count(*) FROM (SELECT DISTINCT * FROM readings))"
RATES = {
    "late": (
        "SELECT avg((timezone('America/La_Paz', read_at)::DATE > period + INTERVAL 1 MONTH)"
        "::INTEGER) FROM (SELECT DISTINCT * FROM readings)",
        0.02,
    ),
    "null_kwh": (
        "SELECT avg((kwh IS NULL)::INTEGER) FROM (SELECT DISTINCT * FROM readings)",
        0.005,
    ),
    "duplicates": (f"SELECT (count(*) - {DISTINCT}) / {DISTINCT} FROM readings", 0.003),
    "estimated": (
        "SELECT avg((status = 'estimated')::INTEGER) FROM (SELECT DISTINCT * FROM readings)",
        0.04,
    ),
    "voided": (
        "SELECT avg((status = 'voided')::INTEGER) FROM (SELECT DISTINCT * FROM readings)",
        0.002,
    ),
}


@pytest.mark.parametrize("name", sorted(RATES))
def test_irregularity_rates(dataset: Path, name: str) -> None:
    sql, expected = RATES[name]
    assert abs(float(scalar(dataset, sql)) - expected) <= 0.005  # type: ignore[arg-type]


def test_late_readings_are_one_to_twenty_days(dataset: Path) -> None:
    sql = (
        "SELECT min(d), max(d) FROM (SELECT timezone('America/La_Paz', read_at)::DATE "
        "- (period + INTERVAL 1 MONTH)::DATE AS d FROM readings) WHERE d > 0"
    )
    connection = duckdb.connect()
    try:
        path = str(dataset / "readings.parquet").replace("'", "''")
        connection.execute(f"CREATE VIEW readings AS FROM read_parquet('{path}')")
        row = connection.execute(sql).fetchone()
    finally:
        connection.close()
    assert row == (1, 20)


def test_parquet_types(dataset: Path) -> None:
    path = str(dataset / "readings.parquet").replace("'", "''")
    connection = duckdb.connect()
    try:
        types = dict(
            connection.execute(
                f"SELECT column_name, column_type FROM (DESCRIBE FROM read_parquet('{path}'))"
            ).fetchall()
        )
    finally:
        connection.close()
    assert types["kwh"] == "DECIMAL(12,3)"
    assert types["read_at"] == "TIMESTAMP WITH TIME ZONE"
    assert types["period"] == "DATE"


def test_null_kwh_is_never_zero(dataset: Path) -> None:
    assert scalar(dataset, "SELECT count(*) FROM readings WHERE kwh IS NULL") > 0  # type: ignore[operator]
    assert scalar(dataset, "SELECT count(*) FROM readings WHERE kwh = 0") == 0


def test_referential_integrity(dataset: Path) -> None:
    assert scalar(dataset, "SELECT count(*) FROM readings ANTI JOIN meters USING (meter_id)") == 0
    assert (
        scalar(dataset, "SELECT count(*) FROM meters ANTI JOIN consumers USING (consumer_id)") == 0
    )


def test_reading_uses_the_meter_installed_in_its_period(dataset: Path) -> None:
    sql = (
        "SELECT count(*) FROM readings JOIN meters USING (meter_id) "
        "WHERE period < installed_on OR (removed_on IS NOT NULL AND period >= removed_on)"
    )
    assert scalar(dataset, sql) == 0


def test_dimensions(dataset: Path) -> None:
    assert scalar(dataset, "SELECT count(DISTINCT zone_id) FROM consumers") == 40
    assert scalar(dataset, "SELECT list_sort(list(DISTINCT tariff)) FROM consumers") == [
        "commercial",
        "industrial",
        "public_lighting",
        "residential",
    ]
    assert scalar(dataset, "SELECT list_sort(list(DISTINCT status)) FROM consumers") == [
        "active",
        "disconnected",
        "suspended",
    ]
    assert scalar(dataset, "SELECT count(*) FROM meters WHERE removed_on IS NOT NULL") == 300


@pytest.mark.parametrize(("size", "seed"), [("medium", 1), ("small", -1)])
def test_invalid_arguments_are_rejected(size: str, seed: int, tmp_path: Path) -> None:
    with pytest.raises(SyntheticError) as raised:
        generate(size, seed, tmp_path)
    assert raised.value.code == "SYNTHETIC_ARGUMENT_INVALID"


def test_non_empty_output_needs_overwrite(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SyntheticError) as raised:
        generate("small", 1, tmp_path)
    assert raised.value.code == "SYNTHETIC_OUTPUT_NOT_EMPTY"
    assert (tmp_path / "keep.txt").exists()
    generate("small", 1, tmp_path, overwrite=True)
    assert not (tmp_path / "keep.txt").exists()


def test_command_line_reports_the_error_code(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.synthetic.generate",
            "--size",
            "huge",
            "--out",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "SYNTHETIC_ARGUMENT_INVALID" in result.stdout
