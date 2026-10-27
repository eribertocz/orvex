# Transformation (`transform` module)

Turns sources into clean, tested tables in DuckLake. Design principle: **the SQL file is pure SQL; configuration lives beside it.**

## 1. References studied

| Tool | Adopted | Rejected | Improved |
|---|---|---|---|
| **Rill** | Incremental strategies `append`, `merge`, `partition_overwrite`; `unique_key`; `on_schema_change`; cron with time zone | SQL embedded as a string inside YAML for advanced models; rewriting a simple model into another format to make it incremental; hand-written `state` queries and `{{if incremental}}` branches | SQL stays in its `.sql`; a model becomes incremental by **adding** a sidecar; the incremental filter is injected automatically |
| **dbt** | Configuration vocabulary (`materialized`, `unique_key`, tests such as `unique`, `not_null`) | Jinja templating inside SQL | Plain SQL; DuckDB-native `$since` parameter when manual control is needed |
| **SQLMesh** | Incremental by time with `lookback` for late data; automatic time-range safety filter; dependency detection from SQL | Proprietary `MODEL (...)` header that is not valid SQL; external cron needed to run | Scheduler built in; scheduling derived from declared freshness |

## 2. Connections

`connections.yaml` declares sources. Secrets are references to environment variables, never literal values.

```yaml
version: 1
connections:
  production:
    type: postgres                 # parquet | csv | duckdb | sqlite | postgres | clickhouse
    host: db.example.internal
    port: 5432
    database: billing
    user: readonly_bi
    password: ${env:ORVEX_PRODUCTION_PASSWORD}
    read_only: true                # required for production databases; enforced by the adapter
  meters_archive:
    type: parquet
    path: /data/archive/readings/*.parquet
```

| Type | Mechanism |
|---|---|
| `parquet`, `csv` | DuckDB native readers |
| `duckdb` | `ATTACH ... (READ_ONLY)` |
| `sqlite`, `postgres` | DuckDB official extensions, attached read-only |
| `clickhouse` | `clickhouse-connect`, the official Python client maintained by ClickHouse, Inc.: **extraction models** written in ClickHouse SQL run on ClickHouse and stream Arrow record batches into DuckDB (§3.5) |

For every type except `clickhouse`, a connection name becomes a SQL schema in DuckDB: `production.readings` reads table `readings` from connection `production`, and DuckDB pushes filters and projections down to the source. ClickHouse has no DuckDB extension that meets the dependency policy (ADR-0002), so it is reached only through extraction models.

**One dialect per engine, no transpilation (ADR-0026).** Orvex never translates SQL between dialects. A model's SQL is DuckDB SQL, except extraction models, whose SQL is the source's own dialect and runs on the source.

## 3. Models

### 3.1 Files and naming

```
models/
└── staging/
    ├── readings.sql      # required: one SELECT statement, pure DuckDB SQL
    └── readings.yaml     # optional sidecar
```

- The table name is `<folder>.<file stem>`: `models/staging/readings.sql` ⇒ `staging.readings`.
- A model folder name must not equal a connection name (validation error `MODEL_SCHEMA_CONFLICT`).
- A `.yaml` without a matching `.sql` is a validation error (`ORPHAN_MODEL_CONFIG`).
- Without a sidecar the model is `materialized: table`, fully rebuilt on each run.
- Dependencies are detected by parsing the SQL with DuckDB's own parser (`json_serialize_sql`). No `ref()` function exists or is needed.
- The engine never rewrites a `.sql` file; it executes a modified copy in memory when it injects filters.

### 3.2 Sidecar reference (`contracts/model-config/v1`)

```yaml
version: 1
description: Meter readings, one row per reading
owner: eriberto
connection: production               # only for extraction models (§3.5); omitted ⇒ DuckDB SQL
materialized: incremental            # view | table | incremental        (default: table)

incremental:
  strategy: merge                    # append | merge | delete_insert | partition_overwrite
  unique_key: [reading_id]           # required for merge and delete_insert
  time_column: updated_at            # watermark column, must exist in the output
  lookback: 1h                       # re-read window for late-arriving rows (default: 0s)
  partition_by: [reading_month]      # required for partition_overwrite
  filter: auto                       # auto | manual                     (default: auto)

freshness: 10m                       # maximum allowed staleness; drives scheduling (minimum 5m)
full_refresh:                        # periodic full rebuild; captures deletions at the source
  cron: "0 3 * * 0"
  time_zone: America/La_Paz

on_schema_change: fail               # fail | append_new_columns | sync_all_columns  (default: fail)

tests:
  - unique: [reading_id]
  - not_null: [reading_id, consumer_id, read_at]
  - accepted_range: { column: kwh, min: 0 }
  - accepted_values: { column: status, values: [valid, estimated, voided] }
  - relationship: { column: consumer_id, to: staging.consumers, field: consumer_id }

reconciliation:                      # compares the model with its source after each run
  - name: kwh_by_month
    measure: sum(kwh)
    group_by: [date_trunc('month', read_at)]
    source: production.readings
    tolerance: 0
```

Grammar:

- **Durations:** `<positive integer><unit>`, with unit `s`, `m`, `h` or `d` (for example `90s`, `10m`, `1h`, `7d`).
- **Cron:** standard five-field cron expression; `time_zone` is an IANA name (default: the project time zone in `project.yaml`).
- Unknown keys are validation errors, never ignored.
- **Strategies:**
  - `append` inserts the loaded rows. It requires `lookback: 0s` (re-reading a window would duplicate rows); `lookback` greater than zero with `append` is rejected with `LOOKBACK_REQUIRES_UNIQUE_KEY` (use `merge`).
  - `merge` updates rows whose `unique_key` exists and inserts the rest.
  - `delete_insert` deletes the target rows whose `unique_key` appears in the loaded rows, then inserts the loaded rows (useful when a key's rows are replaced as a set).
  - `partition_overwrite` replaces every target partition (`partition_by`) present in the loaded rows.
- `time_column` must have a temporal type (`DATE`, `TIMESTAMP`, `TIMESTAMPTZ`); otherwise `TIME_COLUMN_NOT_TEMPORAL`.
- The model kind `python` is reserved for Python models after v1.0.0 (ADR-0023); in v1.0.0 a `.py` model is rejected with `UNSUPPORTED_MODEL_KIND`.

### 3.3 Incremental filter

| `filter` | Behavior |
|---|---|
| `auto` | The engine parses the SQL and, when it can prove the `time_column` comes directly from a single source table without aggregation, window functions or set operations in between, injects `<time_column> > $since` on that source. If it cannot prove it, validation fails with `INCREMENTAL_FILTER_NOT_PROVABLE` and asks for `filter: manual`. It never guesses. |
| `manual` | The SQL must reference the DuckDB 2.0 parameter `$since` exactly where the filter belongs. Validation fails with `MISSING_SINCE_PARAMETER` if it is absent. The same SQL runs in any DuckDB 2.0 console after `SET VARIABLE since = TIMESTAMP '…'`. |

Run-time values:

- `$since` = last watermark − `lookback`. On a full refresh (and on the first run), `$since` is the lowest value of the `time_column` type: `-infinity` for DuckDB `DATE`/`TIMESTAMP`/`TIMESTAMPTZ`; for ClickHouse extraction models, the minimum of the declared parameter type (`1900-01-01 00:00:00` for `DateTime64`). The **same SQL** serves full and incremental runs.
- The new watermark is `max(time_column)` **of the rows loaded**, never the wall clock, to avoid clock skew between servers. A run that loads zero rows keeps the previous watermark.
- `$since` is always passed as an explicit prepared-statement parameter, never through `SET VARIABLE` on a shared connection.

### 3.4 Deletions at the source

A watermark on `updated_at` cannot see rows deleted at the source. Orvex handles this explicitly:

- `full_refresh.cron` rebuilds the model periodically.
- `reconciliation` detects divergence between source and model and raises an alert.
- Change-data-capture sources are out of scope for v1.0.0.

### 3.5 Extraction models (ClickHouse)

An extraction model brings data from a source that DuckDB cannot attach. It is a normal model pair whose sidecar names the connection:

```yaml
# models/raw/ch_events.yaml
connection: analytics_ch            # a connection of type clickhouse
materialized: incremental
incremental:
  strategy: merge
  unique_key: [event_id]
  time_column: event_time
  lookback: 10m
  filter: manual
```

```sql
-- models/raw/ch_events.sql — ClickHouse SQL, runs on ClickHouse
SELECT event_id, meter_id, event_time, toDecimal64(value, 4) AS value
FROM events
WHERE event_time > {since:DateTime64(6, 'UTC')}
```

- The SQL is sent unchanged to ClickHouse with the read-only setting (`readonly = 1`); `$since` is a ClickHouse **server-side parameter** (`{since:Type}`), never string-formatted.
- Results stream as Arrow record batches (`query_arrow_stream`) straight into the DuckDB write of the run; no rows are collected in Python (ADR-0025).
- Only `filter: manual` is allowed (Orvex does not parse ClickHouse SQL); a missing `{since:…}` parameter in an incremental extraction model fails with `MISSING_SINCE_PARAMETER`.
- Extraction models have no model dependencies (they read only their connection) and must be one `SELECT`; validation asks ClickHouse to parse it (`EXPLAIN SYNTAX`) and fails with `INVALID_MODEL_SQL` if it does not.
- Type mapping ClickHouse → Arrow → DuckDB is explicit and tested: `Decimal` stays `DECIMAL`, `UInt64` becomes `UBIGINT`, `DateTime64` keeps its time zone, `Nullable` keeps `NULL`. An unmappable type fails with `UNSUPPORTED_SOURCE_TYPE`.
- Downstream models are DuckDB SQL over the extracted table, like any other model.

## 4. Runs: write–audit–publish

Each run of a model executes inside **one DuckLake transaction**:

1. Read the source (full or incremental).
2. Write into the target table within the transaction, entirely inside DuckDB (`INSERT … SELECT`, `MERGE`); rows never pass through Python (ADR-0025).
3. Run the model's `tests` and `reconciliation` inside the same transaction.
4. **All pass** ⇒ commit. DuckLake creates one snapshot; dashboards see the new data atomically.
5. **Any fails** ⇒ rollback. Consumers keep reading the last good snapshot; the run record marks the failure, and the UI shows a visible warning on affected dashboards (law 4, anti-pattern A6).

Every run produces a **run record** (`contracts/run-record/v1`): run id, model, trigger (manual, schedule, freshness, full refresh), project commit hash, DuckDB and module versions, `$since`, rows read and written, watermark, test and reconciliation results, DuckLake snapshot id, timings and outcome. Every run also emits OpenLineage events (start, complete, fail) with input and output datasets.

## 5. Scheduling

- **Freshness-driven:** for every model with `freshness`, the scheduler computes when it must run so that it is never staler than declared, and runs its upstream models first in dependency order. Models without `freshness` run when a downstream model needs them or on manual request.
- The minimum `freshness` is **5 minutes**; a smaller value is rejected with `FRESHNESS_TOO_SHORT` (production sources are read-only replicas or primaries that must not be polled harder).
- **Full refresh** runs on its cron.
- Runs of the same model never overlap; independent models run in parallel up to a configured limit.
- A failed run retries with exponential backoff up to a configured limit, then alerts.
- After each successful commit, `transform` publishes the run record; `semantic` invalidates cached results only for datasets depending on the changed tables.

## 6. Warehouse (DuckLake)

| Mode | Catalog | Data files |
|---|---|---|
| Local | DuckDB file inside `.<slug>/lake/` (single client, recommended by DuckLake for local single-user use). Only one process opens it: CLI commands (`orvex run`, `orvex validate`, and the stdio MCP server `orvex mcp`) talk to the running instance over its local API when one is running; otherwise they open the warehouse themselves, and a second opener fails with `PROJECT_LOCKED` | Local Parquet under `.<slug>/lake/data/` |
| Server | PostgreSQL (recommended by DuckLake for multi-user use with concurrent clients) | Local disk or object storage |

- Snapshot retention is configured by the user in `project.yaml` (default 90 days). Retention defines how long receipts stay **exactly** reproducible.
- Snapshot expiration and file compaction are scheduled maintenance tasks with their own run records.

## 7. Error codes (initial set)

| Code | Meaning |
|---|---|
| `MODEL_SCHEMA_CONFLICT` | A model folder has the same name as a connection |
| `ORPHAN_MODEL_CONFIG` | Sidecar without SQL file |
| `INVALID_MODEL_SQL` | The SQL file is not a single valid SELECT statement |
| `DEPENDENCY_CYCLE` | Models depend on each other circularly |
| `INCREMENTAL_FILTER_NOT_PROVABLE` | `filter: auto` cannot be applied safely |
| `MISSING_SINCE_PARAMETER` | `filter: manual` without `$since` |
| `MISSING_UNIQUE_KEY` | `merge` or `delete_insert` without `unique_key` |
| `MISSING_PARTITION_BY` | `partition_overwrite` without `partition_by` |
| `LOOKBACK_REQUIRES_UNIQUE_KEY` | `append` with `lookback` greater than zero |
| `TIME_COLUMN_NOT_TEMPORAL` | `time_column` is not a date or timestamp |
| `PROJECT_LOCKED` | Another process holds the project's local warehouse (§6) |
| `SOURCE_NOT_READ_ONLY` | A database connection (`sqlite`, `postgres`, `duckdb`, `clickhouse`) does not declare `read_only: true`; in v1.0.0 every database source is read-only |
| `SCHEMA_CHANGED` | Output schema changed with `on_schema_change: fail` |
| `MODEL_TEST_FAILED` | A test failed; the run was rolled back |
| `RECONCILIATION_FAILED` | Model and source diverge beyond tolerance |
| `FRESHNESS_TOO_SHORT` | `freshness` below the 5-minute minimum |
| `UNSUPPORTED_SOURCE_TYPE` | A source column type has no exact DuckDB mapping |
| `UNSUPPORTED_MODEL_KIND` | A model kind reserved for after v1.0.0 (for example `.py`, ADR-0023) |

## 8. Pre-aggregation refresh

Pre-aggregations (semantic engine S5) are stored as DuckLake tables and refreshed by `transform`, never by `semantic`; no project file is generated:

- The Builder declares them in the Ossie model (`ORVEX` `preaggregations`, `semantic-engine.md` §3). `semantic` turns each declaration into a definition conforming to `contracts/preaggregation/v1` (target table, DuckDB SQL, base tables, refresh strategy); `server` hands those definitions to `transform` at composition and whenever the model changes.
- `transform` schedules each definition like a model that depends on its base tables: it runs, in its own DuckLake transaction, right after every committed run of a base table, with write–audit–publish.
- Its run record stores, for each base table, the DuckLake snapshot at which that table **last changed**. `semantic` uses a pre-aggregation only when those values equal the last-change snapshots of the base tables at the query's snapshot; otherwise it reads the base tables (correct, slower) and the receipt says so.
- Strategies: `partition_overwrite` by time when the base model is incremental by time; full rebuild otherwise.
