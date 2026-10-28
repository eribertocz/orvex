# `model-config` contract v1

**Producer:** the Builder (hand-written sidecars), read by `transform` (`orvex.transform`). **Consumers:** `transform`; `server` (validation output, CLI).

A model is `models/<folder>/<name>.sql`: one `SELECT` in DuckDB SQL (or, for an extraction model, the source's SQL), building the table `<folder>.<name>`. An optional sidecar `models/<folder>/<name>.yaml` configures it. This contract defines the sidecar (`ModelConfig`, parsed from YAML) and the errors of discovering models (`ModelError`). The full grammar and behavior are in `docs/architecture/transformation.md` §3.

## `ModelConfig`

| Field | Meaning |
|---|---|
| `version` | `1` (required) |
| `description`, `owner` | Free text |
| `connection` | Extraction models only: a `clickhouse` connection; the SQL then runs on it |
| `materialized` | `view`, `table` (default) or `incremental` |
| `incremental` | `strategy` (`append`, `merge`, `delete_insert`, `partition_overwrite`), `time_column`, `unique_key`, `lookback` (default `0s`), `partition_by`, `filter` (`auto` default, `manual`) |
| `freshness` | Maximum staleness, a positive duration |
| `full_refresh` | `cron` (five fields) and optional `time_zone` (default: the project's) |
| `on_schema_change` | `fail` (default), `append_new_columns`, `sync_all_columns` |
| `tests` | `unique`, `not_null`, `accepted_range`, `accepted_values`, `relationship` |
| `reconciliation` | `name`, `measure`, `group_by`, `source`, `tolerance` (default `0`) |

Durations are `<n>s|m|h|d`. Unknown keys are errors. The sidecar is read like the project files: a regular UTF-8 file under 1 MiB, one document, no duplicate keys, anchors, aliases or merge keys, no null values.

New cross-field rules are added by the issues that implement those behaviors, as compatible extensions of the error list.

## `ModelError`

| Code | When |
|---|---|
| `MODEL_PATH_INVALID` | A model not at `models/<folder>/<name>.sql`, or a folder or name that is not `^[a-z][a-z0-9_]{0,62}$` |
| `MODEL_SCHEMA_CONFLICT` | A model folder named like a connection, or `lake` |
| `ORPHAN_MODEL_CONFIG` | A sidecar without its `.sql` |
| `UNSUPPORTED_MODEL_KIND` | A `.py` model (reserved for after v1.0.0, ADR-0023) |
| `INVALID_MODEL_SQL` | A DuckDB-SQL model that is not exactly one `SELECT` (or `WITH … SELECT`) |
| `MODEL_CONFIG_INVALID` | A sidecar that is not valid YAML or does not match `ModelConfig`, or whose `connection` is not a `clickhouse` connection of the project, or an incremental model with `filter: auto` whose SQL already uses `$since` (`path` `/incremental/filter`) |
| `MODEL_REFERENCE_UNKNOWN` | A model reads a table that is neither a model (`<folder>.<name>`) nor a source (`<connection>.<table>`) |
| `DEPENDENCY_CYCLE` | Models depend on each other in a cycle (the message lists it in order) |
| `PROJECT_LOCKED` | Another process holds the project's local lake (`orvex run`) |
| `MISSING_UNIQUE_KEY` | `merge` (or `delete_insert`) without `unique_key` |
| `LOOKBACK_REQUIRES_UNIQUE_KEY` | `append` or `partition_overwrite` with a `lookback` greater than zero (re-read rows are only replaced by key: `merge` or `delete_insert`) |
| `TIME_COLUMN_NOT_TEMPORAL` | `time_column` missing from the output, or not `DATE`, `TIMESTAMP` or `TIMESTAMP WITH TIME ZONE` |
| `SCHEMA_CHANGED` | An incremental model's output columns (names, order, types) differ from its existing table and `on_schema_change` does not allow the change: `fail` allows none; `append_new_columns` only new columns; `sync_all_columns` new and removed columns and exact widenings (signed or unsigned integer to a wider one, `DECIMAL(p,s)` to `DECIMAL(p2,s)` with `p2 > p`, `DATE` to `TIMESTAMP`) |
| `UNIQUE_KEY_NULL` | Loaded rows of a `merge` or `delete_insert` with a `NULL` key part (the run rolls back) |
| `UNIQUE_KEY_DUPLICATED` | Loaded rows of a `merge` with the same key twice (the run rolls back; `delete_insert` allows it) |
| `MISSING_PARTITION_BY` | `partition_overwrite` without `partition_by` |
| `MISSING_SINCE_PARAMETER` | An incremental model with `filter: manual` whose SQL does not use `$since` |
| `INCREMENTAL_FILTER_NOT_PROVABLE` | An incremental model with `filter: auto` whose filter cannot be proven safe (see below), or that is an extraction model; `line` is the time column's line when known, and the message names the reason (use `filter: manual` and place `$since`) |
| `MODEL_TEST_FAILED` | A model test found failing rows after the model was written (the run rolls back; the run record lists every test with its failing-row count) |
| `RECONCILIATION_FAILED` | A reconciliation check found groups where the model and its source differ by more than the tolerance, or exist on one side only (the run rolls back) |
| `FRESHNESS_TOO_SHORT` | `freshness` below `5m` (sources must not be polled more often) |
| `UNSUPPORTED_SOURCE_TYPE` | An extraction model's output column whose ClickHouse type has no exact DuckDB type (see below); the message names each such column and its type, with a hint to cast it in the SQL |
| `SOURCE_CONNECTION_FAILED` | An extraction model's ClickHouse connection cannot be reached or refuses the credentials; the message never contains the password |
| `SOURCE_NOT_READ_ONLY` | An extraction model's ClickHouse user whose profile fixes `readonly` to `0` or `2`: its queries cannot be made read-only (`readonly = 1`) |
| `SOURCE_LIMITS_REFUSED` | An extraction model's ClickHouse user that does not accept the validation limits (`max_execution_time`, `max_rows_to_read`, `read_overflow_mode`), or whose queries could lift them; the message gives the configuration (decision #114) |

## Extraction models (ClickHouse)

A model whose sidecar names a `clickhouse` `connection` is ClickHouse SQL. Validation sends it to ClickHouse, read-only (`readonly = 1`), and does not run it. ClickHouse may evaluate parts of a query while it describes it (scalar and `EXISTS` subqueries, also inside a view the SQL reads), so every validation query carries limits that ClickHouse enforces: `max_rows_to_read = 1` with `read_overflow_mode = 'throw'` and `max_execution_time`. A query that would read rows fails (`INVALID_MODEL_SQL`) after at most one block. A scalar or `EXISTS` subquery written in the model itself is refused before that; subqueries in `FROM`, named `WITH` tables and the right side of `IN` are fine. A model cannot set ClickHouse settings (`SETTINGS …` → `INVALID_MODEL_SQL`).

The ClickHouse user must accept those limits and must not be able to lift them (decision #114). Use a user with only `SELECT` grants, without a settings profile or role that fixes `readonly` or makes these settings changeable in read-only mode (`CHANGEABLE_IN_READONLY`):

```sql
CREATE USER extraction IDENTIFIED BY '…';
GRANT SELECT ON analytics.* TO extraction;
```

When the connection opens, queries check this: one with the limits must run, and one per limit that lifts it (`SETTINGS max_rows_to_read = 1000000000`, `read_overflow_mode = 'break'`, `max_execution_time = 3600`) must be refused as a read-only violation. A view's own `SETTINGS` cannot lift them either. Otherwise the connection is refused (`SOURCE_LIMITS_REFUSED`). A profile that fixes `readonly` to `0` or `2` is refused (`SOURCE_NOT_READ_ONLY`).

Known limits:
- **Engines the limit does not cover.** ClickHouse applies `max_rows_to_read` to MergeTree-family tables. It does not apply it to some other engines: `Memory`, `Log`, `TinyLog`, `File` and `URL` (verified), and possibly integration engines such as `S3`, `PostgreSQL` or `MySQL`. When describing SQL makes ClickHouse read such a table (a scalar subquery in a view the model reads), only the time limit (`max_execution_time`) bounds it.
- **Block size.** The row limit stops a read after the first block. Verified: MergeTree-family tables stopped before reading (or after fewer than 1,000 rows); `Join` and `EmbeddedRocksDB` tables after one ordinary block (about 65,000 rows); `StripeLog` and `Buffer` tables keep the blocks they were written with, so one read can be as large as one insert. `Merge`, `Distributed` and views (also `SQL SECURITY DEFINER` views) pass the limit on to the tables they read and cost what those tables cost.
- **Dictionaries.** Dictionary lookups (`dictGet…`, `joinGet…`) are allowed. Describing them, or a dictionary read as a table, can make ClickHouse load a dictionary that is not in memory yet.

A quoted name with a line break, a control character or a backslash escape is refused, because the checks read ClickHouse's `EXPLAIN AST` text, where names are printed unescaped. Then: `EXPLAIN AST` must show one `SELECT` (otherwise `INVALID_MODEL_SQL` with ClickHouse's message), and `DESCRIBE` gives its output columns. Before that, the text alone is checked (strings, quoted identifiers and comments skipped):

- one statement (a final `;` is allowed) → otherwise `INVALID_MODEL_SQL`;
- incremental models need `filter: manual` (`filter: auto` → `INCREMENTAL_FILTER_NOT_PROVABLE`, reason `extraction`) and `{since:Type}` (→ `MISSING_SINCE_PARAMETER`), with `Type` one of `Date`, `Date32`, `DateTime`, `DateTime('zone')`, `DateTime64(p)`, `DateTime64(p, 'zone')` (→ `MODEL_CONFIG_INVALID`); validation binds the lowest value of that type;
- a non-incremental model must not use `{since:…}`, and no other query parameter can be bound → `INVALID_MODEL_SQL`.

Output types map exactly; anything else is `UNSUPPORTED_SOURCE_TYPE`:

| ClickHouse | DuckDB |
|---|---|
| `Int8` `Int16` `Int32` `Int64` `Int128` | `TINYINT` `SMALLINT` `INTEGER` `BIGINT` `HUGEINT` |
| `UInt8` `UInt16` `UInt32` `UInt64` `UInt128` | `UTINYINT` `USMALLINT` `UINTEGER` `UBIGINT` `UHUGEINT` |
| `Decimal(p, s)`, `p ≤ 38` | `DECIMAL(p,s)` |
| `Float32` `Float64` | `FLOAT` `DOUBLE` |
| `String`, `FixedString(n)`, `LowCardinality(String)`, `Enum8`, `Enum16` | `VARCHAR` |
| `Bool` | `BOOLEAN` |
| `UUID` | `UUID` |
| `Date`, `Date32` | `DATE` |
| `DateTime`, `DateTime('zone')`, `DateTime64(p)` and `DateTime64(p, 'zone')` with `p ≤ 6` | `TIMESTAMPTZ` (the instant; the zone is the column's, or the server's when it declares none) |
| `Nullable(T)` | `T`, nullable |

Refused, for example: `Int256`, `UInt256`, `Decimal` with `p > 38`, `DateTime64` with `p > 6`, arrays, maps, tuples, JSON, geo types, `Nothing`. The password is read from its `${env:NAME}` variable when validation runs (unset → `PROJECT_SECRET_MISSING`, naming the variable).

## The automatic filter (`filter: auto`)

The filter `<table>.<time_column> > $since` is added to the query that reads the time column's base table only when DuckDB's parse tree proves it removes exactly the rows a filter on the model's output would remove: the column comes unchanged (or only renamed) from exactly one table reference, followed through `FROM` subqueries and CTEs used once, and nothing in between changes which rows survive. The `.sql` file is never changed; `orvex validate --show-sql <model>` prints the compiled SQL. Otherwise the model is refused with one reason:

| Reason | For example |
|---|---|
| `aggregation` | `GROUP BY`, `HAVING`, aggregate functions |
| `window` | Window functions, `QUALIFY` |
| `set operation` | `UNION`, `INTERSECT`, `EXCEPT` |
| `limit` | `LIMIT`, `USING SAMPLE` |
| `distinct` | `DISTINCT`, `DISTINCT ON` |
| `outer join` | The table on the side of an outer join that can be missing, `FULL`, `SEMI`/`ANTI` right sides, `ASOF` right side |
| `expression` | The column computed, renamed by position (`AS t(a, b)`), a struct field, or not a column of the table; an item named by the data rather than the SQL text (`unnest(…)`, `COLUMNS(…)` in an expression, `#n`, an unaliased constant) where the column is traced; a traced name that is not a plain identifier (`[A-Za-z_][A-Za-z0-9_]*`) |
| `several tables` | The column could come from more than one reference, a CTE used more than once, `NATURAL` or `POSITIONAL` joins, `query()`/`query_table()`, a `WITH` inside a subquery or CTE, an output name repeated (or, next to a star, one that may be a renamed copy such as `ts_1`) |
| `not found` | The time column is not in the output |
| `rebind` | A safety net: the filtered SQL no longer binds, or its output columns differ from the model's |
| `extraction` | An extraction model (its SQL runs on the source) |

`details` holds `file` (relative to the project folder), and when known `model`, `path` (a JSON pointer into the sidecar) and `line`. Discovery reports every problem of a project at once.

## Compatibility

New optional fields, new enum values and new error codes are compatible. Renaming or removing a field, or changing a default, is breaking (new `v2`).
