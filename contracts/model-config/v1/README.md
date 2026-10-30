# `model-config` contract v1

**Producer:** the Builder (hand-written sidecars), read by `transform` (`orvex.transform`). **Consumers:** `transform`; `server` (validation output, CLI).

A model is `models/<folder>/<name>.sql`: one `SELECT` in DuckDB SQL, building the table `<folder>.<name>`. An optional sidecar `models/<folder>/<name>.yaml` configures it. This contract defines the sidecar (`ModelConfig`, parsed from YAML) and the errors of discovering models (`ModelError`). The full grammar and behavior are in `docs/architecture/transformation.md` §3.

## `ModelConfig`

| Field | Meaning |
|---|---|
| `version` | `1` (required) |
| `description`, `owner` | Free text |
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
| `INVALID_MODEL_SQL` | A model that is not exactly one `SELECT` (or `WITH … SELECT`); a model whose output repeats a column name (names are compared without letter case, as DuckDB stores them; one problem per repeated name); a `partition_overwrite` model whose `$since` is not only in a lower bound of its `time_column` (see "Partitions cut by `$since`"); an `append` model whose filter can read a row twice (see "Rows read once (`append`)") |
| `MODEL_CONFIG_INVALID` | A sidecar that is not valid YAML or does not match `ModelConfig`, or an incremental model with `filter: auto` whose SQL already uses `$since` (`path` `/incremental/filter`) |
| `MODEL_REFERENCE_UNKNOWN` | A model reads a table that is neither a model (`<folder>.<name>`) nor a source (`<connection>.<table>`) |
| `DEPENDENCY_CYCLE` | Models depend on each other in a cycle (the message lists it in order) |
| `PROJECT_LOCKED` | Another process holds the project's local lake (`orvex run`) |
| `SCHEDULER_ALREADY_RUNNING` | Another `orvex schedule` keeps the project; the message names its process id. One scheduler per project, held by an OS lock released when that process ends (#69) |
| `MISSING_UNIQUE_KEY` | `merge` (or `delete_insert`) without `unique_key` |
| `LOOKBACK_REQUIRES_UNIQUE_KEY` | `append` or `partition_overwrite` with a `lookback` greater than zero (re-read rows are only replaced by key: `merge` or `delete_insert`) |
| `TIME_COLUMN_NOT_TEMPORAL` | `time_column` missing from the output, or not `DATE`, `TIMESTAMP` or `TIMESTAMP WITH TIME ZONE` |
| `SCHEMA_CHANGED` | An incremental model's output columns (names, order, types) differ from its existing table and `on_schema_change` does not allow the change: `fail` allows none; `append_new_columns` only new columns; `sync_all_columns` new and removed columns and exact widenings (signed or unsigned integer to a wider one, `DECIMAL(p,s)` to `DECIMAL(p2,s)` with `p2 > p`, `DATE` to `TIMESTAMP`) |
| `UNIQUE_KEY_NULL` | Loaded rows of a `merge` or `delete_insert` with a `NULL` key part (the run rolls back) |
| `UNIQUE_KEY_DUPLICATED` | Loaded rows of a `merge` with the same key twice (the run rolls back; `delete_insert` allows it) |
| `MISSING_PARTITION_BY` | `partition_overwrite` without `partition_by` |
| `PARTITION_INCOMPLETE` | A `partition_overwrite` run whose `$since` filter loaded only part of a partition: the table holds rows of a loaded partition older than its oldest loaded row (or with a `NULL` time column the loaded rows do not have). The run rolls back; the message names up to 5 such partitions (`NULL` written `NULL`) and their total, and suggests loading whole partitions (see below) |
| `MISSING_SINCE_PARAMETER` | An incremental model with `filter: manual` whose SQL does not use `$since` |
| `INCREMENTAL_FILTER_NOT_PROVABLE` | An incremental model with `filter: auto` whose filter cannot be proven safe (see below); `line` is the time column's line when known, and the message names the reason (use `filter: manual` and place `$since`) |
| `MODEL_TEST_FAILED` | A model test found failing rows after the model was written (the run rolls back; the run record lists every test with its failing-row count) |
| `RECONCILIATION_FAILED` | A reconciliation check found groups where the model and its source differ by more than the tolerance, or exist on one side only (the run rolls back) |
| `FRESHNESS_TOO_SHORT` | `freshness` below `5m` (sources must not be polled more often) |
| `LINEAGE_NOT_SENT` | Run output only, not a `ModelError`: a warning printed after the run when its OpenLineage events could not be built, written to `.<slug>/lineage/`, or posted to the configured collector (an unset key variable, a collector that cannot be reached, a refused or redirected request). The run's outcome does not change. The message names where the problem is (the events file, the lineage folder or `project.yaml`) and the kind of failure, never the key or the collector's address |

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

`details` holds `file` (relative to the project folder), and when known `model`, `path` (a JSON pointer into the sidecar) and `line`. Discovery reports every problem of a project at once.

## Rows read once (`append`)

An `append` model inserts every row it reads, and replaces nothing, so a filter that reads a row again inserts it twice (#135). In an `append` model with `filter: manual`, every `$since` must be a **strict lower bound of the time column by the parameter alone**: `time_column > $since` (or `$since < time_column`). The bound follows the row-path rules of "Partitions cut by `$since`" below (the query holding it returns the model's rows, joined to its `WHERE` by `AND` only; no `UNION`, join, grouping, window or limit on the path; the time column passed through unchanged), with three differences: `>=` is refused (it reads the rows at the watermark again), no expression may surround the parameter (`$since - INTERVAL 1 HOUR` is a hand-written lookback), and `OR time_column IS NULL` is refused (it reads every undated row in every run).

The time column must also have the same value for a row in every run, or a row read once comes back with a later time and is inserted again. So it must be traced from the model's output to table columns, computed on the way only by functions the engine marks as giving the same value in every query (DuckDB's `CONSISTENT` functions, less `current_localtimestamp()`, `current_localtime()`, `version()` and the order-dependent `any_value`, `first`, `last`, `first_value`, `last_value`, `nth_value`): no window function, no subquery, none of `now()`, `random()`, `current_timestamp` and the like. Anything the check cannot trace (it follows aliases, stars, `REPLACE`, subqueries, CTEs and joins) is refused: column lists that rename by position (`WITH c(a, b)`, `AS q(a, b)`), `RENAME`, `COLUMNS(…)`, positional references (`#2`), struct fields, `unnest`, aggregate functions (`max`, `list`, `arg_max`: a group's value changes as rows join it), table functions (`read_parquet` included), `VALUES`, set operations, three-part names (`feed.data.read_at`).

The check reads the model's own SQL: a time column the source computes on each read (a `now()` in an upstream view or model, or a source view) is out of its reach. Anything else is `INVALID_MODEL_SQL` at the model's SQL file before anything runs; the message says what to write (`WHERE <time_column> > $since`) and points to `strategy: merge` when rows can be read again. `filter: auto` already writes `time_column > $since`.

**Rows kept** (#183). A row left out of a run, at or before the watermark, is never read again, so the queries the time column's trace passes through must keep every row they read: no `LIMIT`, `OFFSET` or sample (also on a joined table), no `DISTINCT ON`, `QUALIFY` or `LIMIT … BY`, no window function (a query above could choose one row per key with it, as `QUALIFY` does: `row_number() OVER (…) AS rn` … `WHERE rn = 1`), and no subquery that chooses rows by reading others (in the select list, `WHERE`, `HAVING` or a join's `ON`). Plain `DISTINCT`, `GROUP BY` of the time column passed through, inner joins, `WITH x AS (…)` and `ORDER BY` are accepted. The query that holds the filter itself still reads one table or subquery, not a join (see above). For "the latest row per key", use `merge`.

**Joins** (#189). A join that can give a row another time when other rows arrive makes the row look new, so it is read and inserted again. The time column's trace may pass only through plain inner joins (`JOIN`, `INNER JOIN`, `CROSS JOIN`, `NATURAL JOIN`): `LEFT`, `RIGHT`, `FULL`, `SEMI`, `ANTI`, `ASOF` and `POSITIONAL` joins on it are refused.

**Depth.** A model whose queries and expressions nest more than 64 levels deep (a subquery, a function or an operator such as `+` is one level) is refused (`INVALID_MODEL_SQL`, "too deeply nested to check"), for `append` and `partition_overwrite`, rather than checked until a limit is hit. DuckDB itself refuses SQL nested beyond its parser's depth.

Limit: a row that reaches the source later with a time equal to or older than the watermark (late data) is not read by an `append` model under any filter. Use `merge` with a `lookback` for such sources.

## Partitions cut by `$since` (`partition_overwrite`)

A `partition_overwrite` run deletes every table row of each partition it loads, then inserts the loaded rows. With a `$since` filter, a run can load only part of a partition; the delete would then lose the rest. Two rules prevent it (#155, decision #128 option B).

**At validation: `$since` only sets a lower bound of the time column.** In a `partition_overwrite` model with `filter: manual`, every `$since` must be in a comparison `time_column > x` or `time_column >= x` (or `x < time_column`, `x <= time_column`):
- the comparison is a condition of the `WHERE` clause of a query whose rows are the model's rows, joined to it by `AND` only. Such a query is the model's query, or a query it reads whole as its only `FROM` (a subquery, or a CTE read nowhere else, and in a statement without `query(…)` or `query_table(…)`, which read relations by name), and it reads one table or subquery, not a join. A `$since` under a `UNION` is refused: another branch can load an older row of a partition (another table, an older bound, or no bound), which would hide a cut from the run's check. Columns are followed by name, so nothing on the path may rename them by position: no column list on a subquery, table or CTE (`AS t(a, b)`, `WITH c(a, b) AS …`). `OR time_column IS NULL` may follow the comparison, to reload undated rows too;
- every query on that path returns the time column **unchanged** and keeps its rows as they are: the column is in its select list as itself (`day`, `t.day`, `day AS day`, or a plain `*` / `t.*`), no other output is named like it, and the query has no `GROUP BY`, `HAVING`, window function, `QUALIFY`, `LIMIT`, `OFFSET`, `LIMIT BY`, `DISTINCT ON` or sample (plain `DISTINCT` and `ORDER BY` are allowed). One exception: the query holding the bound may rename or compute the time column when the bound names it without a qualifier and the name means that alias, so the bound applies to the output column. That is so only when that query reads one table that has no column of that name (`SELECT *, CAST(read_at AS DATE) AS day FROM feed.data WHERE day >= $since`): DuckDB reads such a name as the table's column first. Validation looks the table's columns up; a CTE or subquery there, or an expression that itself reads a column of that name (`CAST(read_at AS DATE) AS read_at`), is refused;
- `x` uses `$since` and constants but no column and no random value (`date_trunc('month', $since)`, `$since - INTERVAL 3 DAY`; not `random()`);
- `time_column` is the column itself, in any letter case; a qualifier, if written, names that query's table (its alias or name).

Anything else is `INVALID_MODEL_SQL` before anything runs. That includes a filter on a finer column (`read_at > $since` with `time_column: day`), a function of the time column (`date_trunc('month', read_at) >= …`), an upper bound (`< $since + …`, `BETWEEN`), an `OR` or `NOT` around the bound, a bound that reads another column (`greatest($since, read_at)`), a `$since` in an `IN`, `EXISTS` or scalar subquery, in `EXCEPT`/`INTERSECT`, or in a query that joins, a time column computed from the filtered one (`CAST(read_at AS DATE) AS read_at`, `max(read_at) AS read_at`), rows computed over the filtered rows (`GROUP BY`, a window, a CTE read again in a subquery), and a `$since` outside a comparison. Each of these can drop rows in the middle or at the end of a partition, which no check of the loaded rows can see. The rule is read from DuckDB's parse tree, never from the text.

**In each run: the oldest-row rule.** An incremental run with `$since` (not a first run, not a full refresh) checks, inside its transaction and before the delete, each loaded partition (`partition_by` values compared with `IS NOT DISTINCT FROM`, so `NULL` is a partition). It is cut when:

- the table has dated rows of it older than the oldest dated loaded row of it, or the loaded rows of it have no dated row; or
- the table has rows of it with a `NULL` time column, and the loaded rows of it have none (a `$since` filter never reloads those).

Either fails the run with `PARTITION_INCOMPLETE` and nothing is written. With the filter a lower bound (validation), a run loads every row of a partition from some time on, so any partition it loads in part has older rows left in the table, and fails: `> $since`, `>= $since` and a hand-written lookback all do. Load whole partitions instead, for example `WHERE read_at >= date_trunc('month', $since)` for monthly partitions. A model whose SQL does not use `$since` reloads everything it selects and is not checked. The message quotes text values (`'2026-10'`), writes SQL `NULL` as `NULL`, and shortens values longer than 60 characters.

Limits of the check:
- It looks only at what the table and the loaded rows contain, so a source that deleted any of a partition's oldest rows fails it too. `--full-refresh` then rebuilds the table from what the model selects now. If the source keeps only recent data, that also removes every older partition the source no longer has.
- A table row with the same time as the oldest loaded row, but not reloaded, is not detected. With the filter a lower bound, only another condition of the `WHERE` clause can leave such a row out.
- A model that joins and filters by `$since` is refused.

## Compatibility

New optional fields, new enum values and new error codes are compatible. Renaming or removing a field, or changing a default, is breaking (new `v2`).
