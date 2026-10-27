# Synthetic electricity-distribution dataset

A reproducible dataset of consumers, meters, monthly readings, invoices, payments and credit notes with the irregularities of real data (P0-09, P0-10). Every later phase tests and measures against it, so real company data never enters the repository (ADR-0020).

**Every name, identifier and value is invented.** No row comes from, or describes, a real company, person or place beyond the time zone `America/La_Paz`.

## Generate

```bash
python -m tools.synthetic.generate --size small --seed 42 --out data/small
python -m tools.synthetic.generate --size large --seed 42 --out data/large --overwrite
```

| Option | Meaning |
|---|---|
| `--size` | `small` (default) or `large` |
| `--seed` | Any integer ≥ 0 (default 42) |
| `--out` | Output folder; refused when it is not empty |
| `--overwrite` | Empty the output folder first |

Errors exit with status 2 and print the code: `SYNTHETIC_ARGUMENT_INVALID` (unknown size, negative seed), `SYNTHETIC_OUTPUT_NOT_EMPTY`, or `SYNTHETIC_PRICES_INVALID` (`prices.csv` missing, unreadable, or without exactly one positive price per tariff; checked before anything is written).

## How it works

The generator is DuckDB SQL over `range()`; no row passes through Python (ADR-0025). Every value derives from `hash(seed, tag, row)`, so the same seed and size give **byte-identical** Parquet files, whatever the Python version (3.15, 3.14) or the number of threads. Irregularities whose count changes the number of rows (meter replacements, duplicates, invoice outcomes, credit notes) are chosen through a seeded permutation, so their counts are exact and every seed gives the same row counts. Each output table is written straight from its query to Parquet; intermediate tables are dropped as soon as they are used, and DuckDB spills to a temporary folder outside the output.

## Output

One Parquet file per table, plus `manifest.json` with the size, seed, last period, row counts and a SHA-256 per file.

### `consumers`

| Column | Type | Values |
|---|---|---|
| `consumer_id` | VARCHAR | `C0000000` … |
| `zone_id` | VARCHAR | `Z00` … `Z39` (40 zones) |
| `tariff` | VARCHAR | `residential` (~85%), `commercial` (~12%), `industrial` (~2.5%), `public_lighting` (~0.5%) |
| `connected_on` | DATE | 2000-01-01 to 2019-03-01 |
| `status` | VARCHAR | `active` (~94%), `suspended` (~4%), `disconnected` (~2%) |

### `meters`

| Column | Type | Values |
|---|---|---|
| `meter_id` | VARCHAR | `M0000000-1`; a replacement is `-2` |
| `consumer_id` | VARCHAR | References `consumers` |
| `installed_on` | DATE | Connection date, or the replacement date |
| `removed_on` | DATE, nullable | Set on replaced meters (exactly 3% of consumers) |

### `readings`

One row per consumer and month, read from the meter installed in that month, plus the duplicates.

| Column | Type | Values |
|---|---|---|
| `reading_id` | VARCHAR | `R0000000-202609` |
| `meter_id` | VARCHAR | References `meters` |
| `period` | DATE | First day of the month; the last period is 2026-09-01 |
| `read_at` | TIMESTAMPTZ | On the first day after the period, 08:00–17:59 in `America/La_Paz` |
| `kwh` | DECIMAL(12,3), nullable | Around a typical value per tariff; `NULL` is never 0 |
| `status` | VARCHAR | `valid`, `estimated`, `voided` |

### `invoices`

One per consumer and month.

| Column | Type | Values |
|---|---|---|
| `invoice_id` | VARCHAR | `I0000000-202609` |
| `consumer_id` | VARCHAR | References `consumers` |
| `period` | DATE | The billed month |
| `issued_at` | TIMESTAMPTZ | Two days after the month ends, 09:00–16:59 in `America/La_Paz` |
| `due_on` | DATE | 20 days after the issue date |
| `amount` | DECIMAL(14,2) | Billed kWh × the tariff's price in [`prices.csv`](prices.csv), rounded to cents |
| `status` | VARCHAR | `issued`, `voided` |
| `estimated` | BOOLEAN | `true` when the month's reading is `NULL` or voided: billed from the most recent usable earlier month, or the tariff's typical consumption when there is none |

### `payments`

| Column | Type | Values |
|---|---|---|
| `payment_id` | VARCHAR | `I0000000-202609-1` … `-3` |
| `invoice_id` | VARCHAR | References `invoices` |
| `paid_at` | TIMESTAMPTZ | Within 20 days of issue, or 1 to 30 days after `due_on` when late |
| `amount` | DECIMAL(14,2) | Positive; the payments of an invoice never exceed its amount |
| `channel` | VARCHAR | `bank` (~40%), `cashier` (~35%), `online` (~25%) |

### `credit_notes`

| Column | Type | Values |
|---|---|---|
| `credit_note_id` | VARCHAR | `N0000000` |
| `consumer_id` | VARCHAR | References `consumers`; exactly 1% of consumers |
| `issued_at` | TIMESTAMPTZ | Any day of the covered months |
| `amount` | DECIMAL(14,2) | Negative, 3 to 12 typical monthly bills; it can leave the consumer's balance negative |

`prices.csv` holds invented prices per kWh (`DECIMAL(10,4)`). Money is integer cents or `DECIMAL` from end to end; no amount passes through a float.

### Irregularities

| Irregularity | Rate | Definition |
|---|---|---|
| Late reading | 2% | `read_at` falls 1 to 20 days after the first day after the period |
| `NULL` kwh | 0.5% | `kwh IS NULL` |
| Exact duplicate | 0.3% (exact) | A second, identical row |
| Estimated | 4% | `status = 'estimated'` |
| Voided | 0.2% | `status = 'voided'` |
| Voided invoice | 0.5% of invoices (exact) | `status = 'voided'`; never paid |
| Unpaid invoice | 8% of invoices (exact) | No payment |
| Partially paid invoice | 7% of invoices (exact) | 1, 2 or 3 payments totalling 20–89% of the amount |
| Fully paid invoice | 84.5% of invoices (exact) | One payment for the whole amount |
| Late payment | 10% of payments | `paid_at` after `due_on` in `America/La_Paz` |
| Estimated invoice | 0.7% of invoices | Follows the `NULL` and voided readings |

Rates other than duplicates are measured on distinct rows. Tests check them within ±0.5 percentage points on `small`; on `large` they fall within ±0.2.

## Sizes

| Size | Consumers | Months | `consumers` | `meters` | `readings` |
|---|---|---|---|---|---|
| `small` | 10,000 | 24 | 10,000 | 10,300 | 240,720 |
| `large` | 1,000,000 | 60 | 1,000,000 | 1,030,000 | 60,180,000 |

`small` takes about half a second; its files total about 3.3 MB.

`large`, measured once (2 vCPU, 8 GB RAM, Linux x86_64, DuckDB 2.0.0, Python 3.15): **1 min 37 s**, files `readings.parquet` 905 MB, `meters.parquet` 12 MB, `consumers.parquet` 7.4 MB. It is not part of the test suite.
