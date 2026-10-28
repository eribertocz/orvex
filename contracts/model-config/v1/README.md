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
| `expression` | The column computed, renamed by position (`AS t(a, b)`), a struct field, or not a column of the table; an item named by the data rather than the SQL text (`unnest(…)`, `COLUMNS(…)` in an expression, `#n`, an unaliased constant) where the column is traced |
| `several tables` | The column could come from more than one reference, a CTE used more than once, `NATURAL` or `POSITIONAL` joins, `query()`/`query_table()`, an output name repeated (or, next to a star, one that may be a renamed copy such as `ts_1`) |
| `not found` | The time column is not in the output |
| `rebind` | A safety net: the filtered SQL no longer binds, or its output columns differ from the model's |
| `extraction` | An extraction model (its SQL runs on the source) |

`details` holds `file` (relative to the project folder), and when known `model`, `path` (a JSON pointer into the sidecar) and `line`. Discovery reports every problem of a project at once.

## Compatibility

New optional fields, new enum values and new error codes are compatible. Renaming or removing a field, or changing a default, is breaking (new `v2`).
