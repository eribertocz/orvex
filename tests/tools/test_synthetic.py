"""P0-09 acceptance tests on the `small` dataset. Every check is SQL over the Parquet files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tools.synthetic.generate import PRICES, TABLES, SyntheticError, generate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("small")
    generate("small", 42, out)
    return out


def scalar(dataset: Path, sql: str) -> object:
    connection = duckdb.connect()
    try:
        for table in TABLES:
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
    assert manifest(dataset)["rows"] == {
        "consumers": 10_000,
        "meters": 10_300,
        "readings": 240_720,
        "invoices": 240_000,
        "payments": 236_400,
        "credit_notes": 100,
    }
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


PAID = "(SELECT invoice_id, count(*) AS n, sum(amount) AS paid FROM payments GROUP BY invoice_id)"
BILLING_RATES = {
    "voided": ("SELECT avg((status = 'voided')::INTEGER) FROM invoices", 0.005),
    "unpaid": (
        f"SELECT avg((status = 'issued' AND p.n IS NULL)::INTEGER) "
        f"FROM invoices LEFT JOIN {PAID} AS p USING (invoice_id)",
        0.08,
    ),
    "partial": (
        f"SELECT avg(coalesce(p.paid < i.amount, false)::INTEGER) "
        f"FROM invoices AS i LEFT JOIN {PAID} AS p USING (invoice_id)",
        0.07,
    ),
    "late_payments": (
        "SELECT avg((timezone('America/La_Paz', paid_at)::DATE > due_on)::INTEGER) "
        "FROM payments JOIN invoices USING (invoice_id)",
        0.10,
    ),
}


@pytest.mark.parametrize("name", sorted(BILLING_RATES))
def test_billing_rates(dataset: Path, name: str) -> None:
    sql, expected = BILLING_RATES[name]
    assert abs(float(scalar(dataset, sql)) - expected) <= 0.005  # type: ignore[arg-type]


def test_money_columns_are_exact_decimals(dataset: Path) -> None:
    for table in ("invoices", "payments", "credit_notes"):
        path = str(dataset / f"{table}.parquet").replace("'", "''")
        connection = duckdb.connect()
        try:
            row = connection.execute(
                f"SELECT column_type FROM (DESCRIBE FROM read_parquet('{path}')) "
                "WHERE column_name = 'amount'"
            ).fetchone()
        finally:
            connection.close()
        assert row == ("DECIMAL(14,2)",)


def test_payments_never_exceed_their_invoice(dataset: Path) -> None:
    sql = (
        f"SELECT count(*) FROM invoices AS i JOIN {PAID} AS p USING (invoice_id) "
        "WHERE p.paid > i.amount"
    )
    assert scalar(dataset, sql) == 0
    assert scalar(dataset, "SELECT count(*) FROM payments WHERE amount <= 0") == 0


def test_voided_invoices_have_no_payments(dataset: Path) -> None:
    sql = "SELECT count(*) FROM payments JOIN invoices USING (invoice_id) WHERE status = 'voided'"
    assert scalar(dataset, sql) == 0


def test_credit_notes_leave_negative_balances(dataset: Path) -> None:
    assert scalar(dataset, "SELECT max(amount) FROM credit_notes") < 0  # type: ignore[operator]
    sql = """
        SELECT count(*) FROM (
            SELECT consumer_id, sum(amount) AS balance FROM (
                SELECT consumer_id, amount FROM invoices WHERE status = 'issued'
                UNION ALL
                SELECT i.consumer_id, -p.amount
                FROM payments AS p JOIN invoices AS i USING (invoice_id)
                UNION ALL
                SELECT consumer_id, amount FROM credit_notes
            ) GROUP BY consumer_id
        ) WHERE balance < 0
    """
    assert scalar(dataset, sql) > 0  # type: ignore[operator]


def test_estimated_invoices_follow_unusable_readings(dataset: Path) -> None:
    sql = """
        SELECT count(*) FROM invoices AS i
        JOIN (SELECT DISTINCT * FROM readings) AS r
            ON r.reading_id = 'R' || substr(i.invoice_id, 2)
        WHERE i.estimated <> (r.kwh IS NULL OR r.status = 'voided')
    """
    assert scalar(dataset, sql) == 0
    assert scalar(dataset, "SELECT count(*) FROM invoices WHERE estimated") > 0  # type: ignore[operator]
    assert scalar(dataset, "SELECT count(*) FROM invoices WHERE amount IS NULL") == 0


def test_billing_integrity(dataset: Path) -> None:
    assert (
        scalar(dataset, "SELECT count(*) FROM payments ANTI JOIN invoices USING (invoice_id)") == 0
    )
    assert (
        scalar(dataset, "SELECT count(*) FROM invoices ANTI JOIN consumers USING (consumer_id)")
        == 0
    )
    assert (
        scalar(dataset, "SELECT count(*) FROM credit_notes ANTI JOIN consumers USING (consumer_id)")
        == 0
    )
    due_gap = "due_on - timezone('America/La_Paz', issued_at)::DATE"
    assert scalar(dataset, f"SELECT count(*) FROM invoices WHERE {due_gap} <> 20") == 0


@pytest.mark.parametrize(
    "content",
    [
        None,
        "tariff,price_per_kwh\nresidential,0.85\ncommercial,1.12\nindustrial,0.74\n",
        "tariff,price_per_kwh\nresidential,0.85\ncommercial,1.12\nindustrial,0.74\npublic_lighting,\n",
        "tariff,price_per_kwh\nresidential,0.85\ncommercial,1.12\nindustrial,0.74\npublic_lighting,abc\n",
        PRICES.read_text(encoding="utf-8") + "residential,0.90\n",
    ],
    ids=["missing", "tariff-without-price", "empty-price", "not-a-number", "duplicate-tariff"],
)
def test_invalid_prices_are_rejected_before_writing(content: str | None, tmp_path: Path) -> None:
    prices = tmp_path / "prices.csv"
    if content is not None:
        prices.write_text(content, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SyntheticError) as raised:
        generate("small", 1, out, overwrite=True, prices=prices)
    assert raised.value.code == "SYNTHETIC_PRICES_INVALID"
    assert sorted(path.name for path in out.iterdir()) == ["keep.txt"]
