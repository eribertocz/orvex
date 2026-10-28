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

Cross-field rules not listed below (`MISSING_PARTITION_BY`, `FRESHNESS_TOO_SHORT`, filter checks) are added by the issues that implement those behaviors, as compatible extensions of the error list.

## `ModelError`

| Code | When |
|---|---|
| `MODEL_PATH_INVALID` | A model not at `models/<folder>/<name>.sql`, or a folder or name that is not `^[a-z][a-z0-9_]{0,62}$` |
| `MODEL_SCHEMA_CONFLICT` | A model folder named like a connection, or `lake` |
| `ORPHAN_MODEL_CONFIG` | A sidecar without its `.sql` |
| `UNSUPPORTED_MODEL_KIND` | A `.py` model (reserved for after v1.0.0, ADR-0023) |
| `INVALID_MODEL_SQL` | A DuckDB-SQL model that is not exactly one `SELECT` (or `WITH … SELECT`) |
| `MODEL_CONFIG_INVALID` | A sidecar that is not valid YAML or does not match `ModelConfig`, or whose `connection` is not a `clickhouse` connection of the project |
| `MODEL_REFERENCE_UNKNOWN` | A model reads a table that is neither a model (`<folder>.<name>`) nor a source (`<connection>.<table>`) |
| `DEPENDENCY_CYCLE` | Models depend on each other in a cycle (the message lists it in order) |
| `PROJECT_LOCKED` | Another process holds the project's local lake (`orvex run`) |
| `MISSING_UNIQUE_KEY` | `merge` (or `delete_insert`) without `unique_key` |
| `LOOKBACK_REQUIRES_UNIQUE_KEY` | `append` with a `lookback` other than `0s` (re-reading would duplicate rows) |
| `TIME_COLUMN_NOT_TEMPORAL` | `time_column` missing from the output, or not `DATE`, `TIMESTAMP` or `TIMESTAMP WITH TIME ZONE` |
| `SCHEMA_CHANGED` | An incremental model's output columns (names, order, types) differ from its existing table |
| `UNIQUE_KEY_NULL` | Loaded rows of a `merge` with a `NULL` key part (the run rolls back) |
| `UNIQUE_KEY_DUPLICATED` | Loaded rows of a `merge` with the same key twice (the run rolls back) |

`details` holds `file` (relative to the project folder), and when known `model`, `path` (a JSON pointer into the sidecar) and `line`. Discovery reports every problem of a project at once.

## Compatibility

New optional fields, new enum values and new error codes are compatible. Renaming or removing a field, or changing a default, is breaking (new `v2`).
