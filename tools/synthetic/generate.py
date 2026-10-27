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
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True, slots=True)
class Size:
    consumers: int
    months: int


SIZES = {"small": Size(consumers=10_000, months=24), "large": Size(consumers=1_000_000, months=60)}
LAST_PERIOD = "2026-09-01"
TABLES = ("consumers", "meters", "readings", "invoices", "payments", "credit_notes")
TARIFFS = ("residential", "commercial", "industrial", "public_lighting")
PRICES = Path(__file__).with_name("prices.csv")


class SyntheticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def prepare(connection: duckdb.DuckDBPyConnection, seed: int, size: Size, temporary: Path) -> None:
    """Session settings and the deterministic random helper `u(tag, row)` in [0, 1)."""
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET temp_directory = $path", {"path": str(temporary)})
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
    connection.execute(f"CREATE MACRO invoice_count() AS {int(size.consumers) * int(size.months)}")


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
CREATE TABLE monthly_readings AS
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
        row_number,
        i,
        typical_kwh
    FROM base
)
FROM readings;

CREATE TABLE readings AS
SELECT reading_id, meter_id, period, read_at, kwh, status
FROM (
    SELECT *, 0 AS copy FROM monthly_readings
    UNION ALL
    SELECT *, 1 AS copy FROM monthly_readings
    WHERE chosen(998244353, 'duplicate', row_number, consumer_count() * month_count(), 0.003)
)
ORDER BY reading_id, copy
"""

# One invoice per consumer and month. A month whose reading is NULL or voided is billed from the
# most recent usable earlier month (the tariff's typical consumption when there is none) and
# flagged `estimated`. Invoice outcomes come from a seeded permutation, so their counts are exact:
# 0.5% voided (never paid), 8% unpaid, 7% partially paid with 1 to 3 payments, the rest paid in
# full. Money is integer cents or DECIMAL throughout; no value passes through a float.
BILLING_SQL = """
CREATE TABLE invoice_facts AS
WITH usable AS (
    SELECT
        r.i,
        r.period,
        r.row_number,
        c.tariff,
        r.typical_kwh,
        r.kwh,
        CASE WHEN r.status <> 'voided' THEN r.kwh END AS usable_kwh
    FROM monthly_readings AS r
    JOIN consumer_tariffs AS c USING (i)
),
billed AS (
    SELECT
        *,
        coalesce(
            usable_kwh,
            last_value(usable_kwh IGNORE NULLS) OVER (
                PARTITION BY i ORDER BY period ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            typical_kwh::DECIMAL(12, 3)
        ) AS billed_kwh,
        usable_kwh IS NULL AS estimated,
        (1000000009::HUGEINT * row_number + hash({seed}, 'invoice_outcome') % invoice_count())
            % invoice_count() AS outcome_rank
    FROM usable
)
SELECT
    b.i,
    b.row_number,
    'I' || lpad(b.i::VARCHAR, 7, '0') || '-' || strftime(b.period, '%Y%m') AS invoice_id,
    b.period,
    timezone(
        'America/La_Paz',
        (b.period + INTERVAL 1 MONTH + INTERVAL 2 DAY)::TIMESTAMP
            + INTERVAL (540 + floor(u('issue_minute', b.row_number) * 480)::INTEGER) MINUTE
    ) AS issued_at,
    (b.period + INTERVAL 1 MONTH + INTERVAL 22 DAY)::DATE AS due_on,
    ROUND(b.billed_kwh * p.price_per_kwh, 2)::DECIMAL(14, 2) AS amount,
    b.estimated,
    CASE
        WHEN b.outcome_rank < round(invoice_count() * 0.005) THEN 'voided'
        WHEN b.outcome_rank < round(invoice_count() * 0.085) THEN 'unpaid'
        WHEN b.outcome_rank < round(invoice_count() * 0.155) THEN 'partial'
        ELSE 'full'
    END AS outcome,
    1 + b.outcome_rank % 3 AS partial_payment_count
FROM billed AS b
JOIN prices AS p USING (tariff);

DROP TABLE monthly_readings;

CREATE TABLE invoices AS
SELECT
    invoice_id,
    'C' || lpad(i::VARCHAR, 7, '0') AS consumer_id,
    period,
    issued_at,
    due_on,
    amount,
    CASE WHEN outcome = 'voided' THEN 'voided' ELSE 'issued' END AS status,
    estimated
FROM invoice_facts
ORDER BY invoice_id;

CREATE TABLE payment_facts AS
WITH planned AS (
    SELECT
        f.invoice_id,
        f.row_number,
        f.issued_at,
        f.due_on,
        CASE WHEN f.outcome = 'full' THEN 1 ELSE f.partial_payment_count END AS payment_count,
        (f.amount * 100)::BIGINT AS invoice_cents,
        CASE
            WHEN f.outcome = 'full' THEN (f.amount * 100)::BIGINT
            ELSE (f.amount * 100)::BIGINT * (20 + floor(u('paid_share', f.row_number) * 70)::BIGINT)
                // 100
        END AS paid_cents
    FROM invoice_facts AS f
    WHERE f.outcome IN ('full', 'partial')
)
SELECT
    p.invoice_id,
    p.row_number * 3 + s.j AS payment_number,
    s.j,
    p.issued_at,
    p.due_on,
    CASE
        WHEN s.j < p.payment_count - 1 THEN p.paid_cents // p.payment_count
        ELSE p.paid_cents - (p.payment_count - 1) * (p.paid_cents // p.payment_count)
    END AS cents
FROM planned AS p
JOIN range(3) AS s(j) ON s.j < p.payment_count;

DROP TABLE invoice_facts;

CREATE TABLE payments AS
SELECT
    invoice_id || '-' || (j + 1)::VARCHAR AS payment_id,
    invoice_id,
    CASE
        WHEN u('late_payment', payment_number) < 0.10
            THEN timezone(
                'America/La_Paz',
                (
                    due_on
                    + INTERVAL (1 + floor(u('payment_late_days', payment_number) * 30)::INTEGER) DAY
                )::TIMESTAMP
                    + INTERVAL (480 + floor(u('pay_minute', payment_number) * 720)::INTEGER) MINUTE
            )
        ELSE timezone(
            'America/La_Paz',
            (timezone('America/La_Paz', issued_at)::DATE
                + INTERVAL (floor(u('pay_day', payment_number) * 21)::INTEGER) DAY)::TIMESTAMP
                + INTERVAL (480 + floor(u('pay_minute', payment_number) * 720)::INTEGER) MINUTE
        )
    END AS paid_at,
    (cents::DECIMAL(14, 2) * 0.01)::DECIMAL(14, 2) AS amount,
    CASE
        WHEN u('channel', payment_number) < 0.40 THEN 'bank'
        WHEN u('channel', payment_number) < 0.75 THEN 'cashier'
        ELSE 'online'
    END AS channel
FROM payment_facts
ORDER BY payment_id;

DROP TABLE payment_facts;

CREATE TABLE credit_notes AS
SELECT
    'N' || lpad(c.i::VARCHAR, 7, '0') AS credit_note_id,
    'C' || lpad(c.i::VARCHAR, 7, '0') AS consumer_id,
    timezone(
        'America/La_Paz',
        (DATE_TRUNC('month', DATE '{last}') - INTERVAL (month_count() - 1) MONTH
            + INTERVAL (floor(u('credit_day', c.i) * month_count() * 30)::INTEGER) DAY)::TIMESTAMP
            + INTERVAL (540 + floor(u('credit_minute', c.i) * 480)::INTEGER) MINUTE
    ) AS issued_at,
    (-ROUND(c.typical_kwh * p.price_per_kwh * (3 + 9 * u('credit_size', c.i)), 2))
        ::DECIMAL(14, 2) AS amount
FROM consumer_tariffs AS c
JOIN prices AS p USING (tariff)
WHERE chosen(999999937, 'credit_note', c.i, consumer_count(), 0.01)
ORDER BY credit_note_id
"""

CONSUMER_TARIFFS_SQL = """
CREATE TABLE consumer_tariffs AS
SELECT
    i,
    CASE
        WHEN u('tariff', i) < 0.85 THEN 'residential'
        WHEN u('tariff', i) < 0.97 THEN 'commercial'
        WHEN u('tariff', i) < 0.995 THEN 'industrial'
        ELSE 'public_lighting'
    END AS tariff,
    CASE
        WHEN u('tariff', i) < 0.85 THEN 150
        WHEN u('tariff', i) < 0.97 THEN 900
        WHEN u('tariff', i) < 0.995 THEN 15000
        ELSE 400
    END AS typical_kwh
FROM range(consumer_count()) AS consumers(i)
"""

PRICES_SQL = """
CREATE TABLE prices AS
FROM read_csv(
    $path,
    header = true,
    columns = {'tariff': 'VARCHAR', 'price_per_kwh': 'DECIMAL(10,4)'}
)
"""

PRICES_PROBLEMS_SQL = """
SELECT count(*) FROM (
    SELECT tariff FROM (VALUES ('residential'), ('commercial'), ('industrial'), ('public_lighting'))
        AS expected(tariff)
    WHERE tariff NOT IN (
        SELECT tariff FROM prices WHERE price_per_kwh IS NOT NULL AND price_per_kwh > 0
    )
    UNION ALL
    SELECT tariff FROM prices GROUP BY tariff HAVING count(*) > 1
)
"""


def load_prices(connection: duckdb.DuckDBPyConnection, prices: Path) -> None:
    """Load `prices.csv` into the `prices` table, or fail with SYNTHETIC_PRICES_INVALID."""
    if not prices.is_file():
        raise SyntheticError("SYNTHETIC_PRICES_INVALID", f"{prices} does not exist")
    try:
        connection.execute(PRICES_SQL, {"path": str(prices)})
        problems = connection.execute(PRICES_PROBLEMS_SQL).fetchone()
    except duckdb.Error as error:
        raise SyntheticError("SYNTHETIC_PRICES_INVALID", f"{prices}: {error}") from error
    if problems is None or problems[0] != 0:
        raise SyntheticError(
            "SYNTHETIC_PRICES_INVALID",
            f"{prices} must give one positive price to each tariff: {', '.join(TARIFFS)}",
        )


def generate(
    size_name: str,
    seed: int,
    out: Path,
    overwrite: bool = False,
    threads: int | None = None,
    prices: Path = PRICES,
) -> dict[str, object]:
    if size_name not in SIZES:
        raise SyntheticError("SYNTHETIC_ARGUMENT_INVALID", f"unknown size '{size_name}'")
    if seed < 0:
        raise SyntheticError("SYNTHETIC_ARGUMENT_INVALID", "the seed must be zero or positive")
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise SyntheticError("SYNTHETIC_OUTPUT_NOT_EMPTY", f"{out} is not empty")
    size = SIZES[size_name]

    # `threads` exists for the test that proves the output does not depend on parallelism.
    connection = duckdb.connect(config={"threads": threads} if threads else {})
    temporary = Path(tempfile.mkdtemp(prefix="synthetic-"))
    try:
        # Prices are checked before anything is written or removed.
        load_prices(connection, prices)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        prepare(connection, seed, size, temporary)
        sql = "\n;\n".join(
            (
                CONSUMERS_SQL,
                CONSUMER_TARIFFS_SQL,
                METERS_SQL,
                READINGS_SQL,
                BILLING_SQL.replace("{seed}", str(int(seed))),
            )
        ).format(last=LAST_PERIOD)
        rows: dict[str, int] = {}
        for statement in (part.strip() for part in sql.split(";")):
            if not statement:
                continue
            header, _, query = statement.partition("\n")
            table = header.removeprefix("CREATE TABLE ").removesuffix(" AS")
            if table in TABLES:
                # Each output table is written straight from its query and never kept in memory.
                # The table name comes from the fixed TABLES tuple; the path is a parameter.
                written = connection.execute(
                    f"COPY ({query}) TO $path (FORMAT parquet)",
                    {"path": str(out / f"{table}.parquet")},
                ).fetchone()
                rows[table] = int(written[0]) if written else 0
            else:
                connection.execute(statement)
    finally:
        connection.close()
        shutil.rmtree(temporary, ignore_errors=True)

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
