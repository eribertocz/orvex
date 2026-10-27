"""Synthetic electricity-distribution dataset (P0-09), generated entirely inside DuckDB.

    python -m tools.synthetic.generate --size small --seed 42 --out data/small [--overwrite]

Every value derives from `hash(seed, tag, row)`, so the same seed and size give byte-identical
Parquet files. No row passes through Python (ADR-0025). Names and values are invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True, slots=True)
class Size:
    consumers: int
    months: int


SIZES = {"small": Size(consumers=10_000, months=24), "large": Size(consumers=1_000_000, months=60)}
LAST_PERIOD = "2026-09-01"
TABLES = ("consumers", "meters", "readings")


class SyntheticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def prepare(connection: duckdb.DuckDBPyConnection, seed: int, size: Size) -> None:
    """Session settings and the deterministic random helper `u(tag, row)` in [0, 1)."""
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET preserve_insertion_order = true")
    connection.execute("SET enable_progress_bar = false")
    # `seed` is a validated integer, interpolated as a number literal into the macro body.
    connection.execute(
        f"CREATE MACRO u(tag, row_number) AS "
        f"(hash({int(seed)}, tag, row_number) % 1000000)::DOUBLE / 1000000"
    )
    # `chosen` selects exactly round(total * rate) rows through the bijection
    # x -> (prime * x + offset) mod total: the prime exceeds every total, so the count of chosen
    # rows does not depend on the seed, while the offset (and so the chosen rows) does.
    connection.execute(
        f"CREATE MACRO chosen(prime, tag, x, total, rate) AS "
        f"(prime::HUGEINT * x + hash({int(seed)}, tag) % total) % total < round(total * rate)"
    )
    connection.execute(f"CREATE MACRO consumer_count() AS {int(size.consumers)}")
    connection.execute(f"CREATE MACRO month_count() AS {int(size.months)}")


CONSUMERS_SQL = """
CREATE TABLE consumers AS
SELECT
    'C' || lpad(i::VARCHAR, 7, '0') AS consumer_id,
    'Z' || lpad(floor(u('zone', i) * 40)::INTEGER::VARCHAR, 2, '0') AS zone_id,
    CASE
        WHEN u('tariff', i) < 0.85 THEN 'residential'
        WHEN u('tariff', i) < 0.97 THEN 'commercial'
        WHEN u('tariff', i) < 0.995 THEN 'industrial'
        ELSE 'public_lighting'
    END AS tariff,
    (DATE '2000-01-01' + floor(u('connected', i) * 7000)::INTEGER) AS connected_on,
    CASE
        WHEN u('status', i) < 0.94 THEN 'active'
        WHEN u('status', i) < 0.98 THEN 'suspended'
        ELSE 'disconnected'
    END AS status
FROM range(consumer_count()) AS consumers(i)
ORDER BY consumer_id
"""

# Exactly 3% of consumers had their meter replaced at a month inside the period range.
METERS_SQL = """
CREATE TABLE meter_changes AS
SELECT
    i,
    (DATE_TRUNC('month', DATE '{last}') - INTERVAL (month_count() - 1) MONTH
        + INTERVAL (1 + floor(u('change_month', i) * (month_count() - 2))::INTEGER) MONTH)::DATE
        AS changed_on
FROM range(consumer_count()) AS consumers(i)
WHERE chosen(1000000007, 'meter_change', i, consumer_count(), 0.03);

CREATE TABLE meters AS
SELECT
    'M' || lpad(c.i::VARCHAR, 7, '0') || '-1' AS meter_id,
    'C' || lpad(c.i::VARCHAR, 7, '0') AS consumer_id,
    (DATE '2000-01-01' + floor(u('connected', c.i) * 7000)::INTEGER) AS installed_on,
    m.changed_on AS removed_on
FROM range(consumer_count()) AS c(i)
LEFT JOIN meter_changes AS m USING (i)
UNION ALL
SELECT
    'M' || lpad(i::VARCHAR, 7, '0') || '-2',
    'C' || lpad(i::VARCHAR, 7, '0'),
    changed_on,
    NULL::DATE
FROM meter_changes
ORDER BY meter_id
"""

READINGS_SQL = """
CREATE TABLE readings AS
WITH grid AS (
    SELECT
        c.i,
        p.month_index,
        c.i * month_count() + p.month_index AS row_number,
        (
            DATE_TRUNC('month', DATE '{last}')
            - INTERVAL (month_count() - 1 - p.month_index) MONTH
        )::DATE AS period
    FROM range(consumer_count()) AS c(i)
    CROSS JOIN range(month_count()) AS p(month_index)
),
base AS (
    SELECT
        g.*,
        CASE
            WHEN m.changed_on IS NOT NULL AND g.period >= m.changed_on
                THEN 'M' || lpad(g.i::VARCHAR, 7, '0') || '-2'
            ELSE 'M' || lpad(g.i::VARCHAR, 7, '0') || '-1'
        END AS meter_id,
        CASE
            WHEN u('tariff', g.i) < 0.85 THEN 150
            WHEN u('tariff', g.i) < 0.97 THEN 900
            WHEN u('tariff', g.i) < 0.995 THEN 15000
            ELSE 400
        END AS typical_kwh
    FROM grid AS g
    LEFT JOIN meter_changes AS m USING (i)
),
readings AS (
    SELECT
        'R' || lpad(i::VARCHAR, 7, '0') || '-' || strftime(period, '%Y%m') AS reading_id,
        meter_id,
        period,
        timezone(
            'America/La_Paz',
            (period + INTERVAL 1 MONTH)::TIMESTAMP
                + INTERVAL (
                    CASE
                        WHEN u('late', row_number) < 0.02
                            THEN 1 + floor(u('late_days', row_number) * 20)::INTEGER
                        ELSE 0
                    END
                ) DAY
                + INTERVAL (480 + floor(u('read_minute', row_number) * 600)::INTEGER) MINUTE
        ) AS read_at,
        CASE
            WHEN u('null_kwh', row_number) < 0.005 THEN NULL
            ELSE ROUND(typical_kwh * (0.6 + 0.8 * u('kwh', row_number)), 3)::DECIMAL(12, 3)
        END AS kwh,
        CASE
            WHEN u('reading_status', row_number) < 0.002 THEN 'voided'
            WHEN u('reading_status', row_number) < 0.042 THEN 'estimated'
            ELSE 'valid'
        END AS status,
        row_number
    FROM base
)
SELECT reading_id, meter_id, period, read_at, kwh, status
FROM (
    SELECT *, 0 AS copy FROM readings
    UNION ALL
    SELECT *, 1 AS copy FROM readings
    WHERE chosen(998244353, 'duplicate', row_number, consumer_count() * month_count(), 0.003)
)
ORDER BY reading_id, copy
"""


def generate(
    size_name: str, seed: int, out: Path, overwrite: bool = False, threads: int | None = None
) -> dict[str, object]:
    if size_name not in SIZES:
        raise SyntheticError("SYNTHETIC_ARGUMENT_INVALID", f"unknown size '{size_name}'")
    if seed < 0:
        raise SyntheticError("SYNTHETIC_ARGUMENT_INVALID", "the seed must be zero or positive")
    if out.exists() and any(out.iterdir()):
        if not overwrite:
            raise SyntheticError("SYNTHETIC_OUTPUT_NOT_EMPTY", f"{out} is not empty")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    size = SIZES[size_name]

    # `threads` exists for the test that proves the output does not depend on parallelism.
    connection = duckdb.connect(config={"threads": threads} if threads else {})
    try:
        prepare(connection, seed, size)
        connection.execute(CONSUMERS_SQL)
        for statement in METERS_SQL.format(last=LAST_PERIOD).split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(READINGS_SQL.format(last=LAST_PERIOD))
        rows: dict[str, int] = {}
        for table in TABLES:
            target = out / f"{table}.parquet"
            # Table names come from the fixed TABLES tuple; the path is passed as a parameter.
            connection.execute(f"COPY {table} TO $path (FORMAT parquet)", {"path": str(target)})
            counted = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
            rows[table] = int(counted[0]) if counted else 0
    finally:
        connection.close()

    manifest: dict[str, object] = {
        "size": size_name,
        "seed": seed,
        "last_period": LAST_PERIOD,
        "rows": rows,
        "sha256": {
            f"{table}.parquet": hashlib.sha256((out / f"{table}.parquet").read_bytes()).hexdigest()
            for table in TABLES
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--size", default="small", help="small or large")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    arguments = parser.parse_args()
    try:
        manifest = generate(arguments.size, arguments.seed, arguments.out, arguments.overwrite)
    except SyntheticError as error:
        print(error)
        return 2
    print(json.dumps(manifest["rows"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
