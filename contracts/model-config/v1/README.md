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
| `INVALID_MODEL_SQL` | A DuckDB-SQL model that is not exactly one `SELECT` (or `WITH … SELECT`); a model whose output repeats a column name (names are compared without letter case, as DuckDB stores them; one problem per repeated name); for ClickHouse extraction models, SQL ClickHouse refuses or that breaks the rules of the extraction section (`{since:…}`, settings, subqueries); a `partition_overwrite` model whose `$since`/`{since:…}` is not only in a lower bound of its `time_column` (see "Partitions cut by `$since`"); an `append` model whose filter can read a row twice (see "Rows read once (`append`)") |
| `MODEL_CONFIG_INVALID` | A sidecar that is not valid YAML or does not match `ModelConfig`, or whose `connection` is not a `clickhouse` connection of the project, or an incremental model with `filter: auto` whose SQL already uses `$since` (`path` `/incremental/filter`) |
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
| `INCREMENTAL_FILTER_NOT_PROVABLE` | An incremental model with `filter: auto` whose filter cannot be proven safe (see below), or that is an extraction model; `line` is the time column's line when known, and the message names the reason (use `filter: manual` and place `$since`) |
| `MODEL_TEST_FAILED` | A model test found failing rows after the model was written (the run rolls back; the run record lists every test with its failing-row count) |
| `RECONCILIATION_FAILED` | A reconciliation check found groups where the model and its source differ by more than the tolerance, or exist on one side only (the run rolls back) |
| `FRESHNESS_TOO_SHORT` | `freshness` below `5m` (sources must not be polled more often) |
| `UNSUPPORTED_SOURCE_TYPE` | An extraction model's output column whose ClickHouse type has no exact DuckDB type (see below); the message names each such column and its type, with a hint to cast it in the SQL |
| `SOURCE_CONNECTION_FAILED` | An extraction model's ClickHouse connection cannot be reached or refuses the credentials; the message never contains the password |
| `SOURCE_QUERY_FAILED` | An extraction model's ClickHouse query failed while it ran, before or after its first rows (for example `throwIf`, a time limit, a broken connection); the message gives ClickHouse's own message and code (`(ClickHouse code N)`) when ClickHouse wrote one, never a credential. The run rolls back |
| `UNSUPPORTED_SOURCE_VALUE` | An extraction model sent text that is not valid UTF-8; the message names the column and the row's position in the stream. The value is never stored altered or dropped: the run rolls back |
| `SOURCE_NOT_READ_ONLY` | An extraction model's ClickHouse user whose profile fixes `readonly` to `0` or `2`: its queries cannot be made read-only (`readonly = 1`) |
| `SOURCE_LIMITS_REFUSED` | An extraction model's ClickHouse user that does not accept the validation limits (`max_execution_time`, `max_rows_to_read`, `read_overflow_mode`), or whose queries could lift them; the message gives the configuration (decision #114). At run time: a profile that does not let the run set its own settings (below) |
| `SOURCE_ZONE_UNKNOWN` | An extraction model's `{since:DateTime…}` read in a zone that is not in the time zone database the engine reads (#201): the zone the type declares, or, when it declares none, the ClickHouse connection's (`SELECT timezone()`: the server's, or the user profile's `session_timezone`); the message names the zone and suggests `'UTC'` |
| `LINEAGE_NOT_SENT` | Run output only, not a `ModelError`: a warning printed after the run when its OpenLineage events could not be built, written to `.<slug>/lineage/`, or posted to the configured collector (an unset key variable, a collector that cannot be reached, a refused or redirected request). The run's outcome does not change. The message names where the problem is (the events file, the lineage folder or `project.yaml`) and the kind of failure, never the key or the collector's address |

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
| `DateTime`, `DateTime('zone')`, `DateTime64(p)` and `DateTime64(p, 'zone')` with `p ≤ 6` | `TIMESTAMPTZ` (the instant; the zone is the column's, or the connection's when it declares none) |
| `Nullable(T)` | `T`, nullable |

Refused, for example: `Int256`, `UInt256`, `Decimal` with `p > 38`, `DateTime64` with `p > 6`, arrays, maps, tuples, JSON, geo types, `Nothing`. The password is read from its `${env:NAME}` variable when validation runs (unset → `PROJECT_SECRET_MISSING`, naming the variable).

### Running an extraction model (#98)

`orvex run` sends the model's SQL to ClickHouse unchanged, as a subquery under a projection that only makes each value travel exactly as Arrow:
- `Int128`, `UInt128`, `Enum8`/`Enum16` and `FixedString` are sent as text, which DuckDB casts back exactly. A `FixedString(n)` value arrives without its trailing zero bytes, ClickHouse's padding (`toString`), so `'ab'` in a `FixedString(4)` loads as `'ab'`;
- `DateTime` is sent as `DateTime64(0)` with its zone;
- each row's position in the stream is added, and text is checked to be UTF-8.

Every run query carries:
- `readonly = 1`;
- `max_execution_time`;
- `cancel_http_readonly_queries_on_client_close = 1`;
- `max_block_size = 65536` and `preferred_block_size_bytes = 16777216`;
- every `*_overflow_mode` setting `'throw'`, `limit = 0` and `offset = 0`, so a limit in the user's profile fails the run (`SOURCE_QUERY_FAILED`) instead of shortening its result;
- `additional_table_filters = {}` and `additional_result_filter = ''`, so row filters in the user's profile do not apply to the run.

A profile that fixes any of these to another value, or constrains it so ClickHouse refuses it, is refused (`SOURCE_LIMITS_REFUSED`, naming the setting when the server lists it as fixed).

`{since:…}` is bound by ClickHouse, never written into the SQL. A type that declares no zone is read in the connection's zone, which the engine asks ClickHouse for (`SELECT timezone()`, with the validation settings; never a client default), so a user profile's `session_timezone` counts too (#201). A zone, declared or the connection's, must be an IANA name of the time zone database bundled with the engine (the list the project's `time_zone` is checked against); any other name, such as `localtime` or `posixrules`, which each machine resolves to its own zone, is `SOURCE_ZONE_UNKNOWN` (#212). The zone's rules are read from that bundled database too, never from the machine's own zone files, which some systems build differently (Ubuntu's `EET` and `WET`). Its value:
- **First run, full refresh, or no stored watermark:** a value no later than the first instant of the declared type, in every zone (measured on ClickHouse 26.9): `1970-01-01` for `Date`; `0000-01-01` for `Date32`; `1900-01-01 00:00:00` for `DateTime`, which ClickHouse reads as its first second, 1970-01-01 00:00:00 UTC, even west of UTC; for `DateTime64(p)` a number of seconds since 1970-01-01 UTC, read as the same instant in every zone: `-62167219200` (0000-01-01 00:00:00 UTC), or `-9223372036.854775808` (1677-09-21, the lowest count of nanoseconds) when `p` is 9. So the first run reads every row of the time column only when the parameter's type starts no later than the column's, in every zone, and the two can be compared there: `{since:Date}` for a `Date` column; `{since:Date32}` for `Date`, `Date32` or `DateTime`; `{since:DateTime…}` for `Date` or `DateTime`; `{since:DateTime64(p)}` with `p` up to 8 for `Date`, `Date32`, `DateTime` or `DateTime64`; `{since:DateTime64(9)}` for `Date` or `DateTime` (wrappers `Nullable(…)`/`LowCardinality(…)` read through; a time column's `DateTime64` has at most 6 digits, the precisions that are mapped). Any other pair is `INVALID_MODEL_SQL` before anything runs, under any strategy, since a first run would skip the column's oldest rows for good, or fail (#179); the column's own type is always accepted, except that an `append` model declares `{since:Date32}` for a `Date` column and `{since:DateTime64(0, 'zone')}` for a `DateTime` one (see "Rows at the default value"). A run whose watermark minus `lookback` falls before the type's first instant binds the same value.
- **Otherwise:** the stored watermark minus `lookback`, written in the parameter's zone (the connection's when it declares none), and truncated, never rounded up: a date for `Date`/`Date32`, whole seconds for `DateTime`, `p` digits for `DateTime64(p)`. A `Date`/`Date32` value is taken in UTC-12, where a date's midnight comes last. ClickHouse compares a date at midnight in the zone of the expression it meets (measured on 26.9), which the SQL decides. Taking it in UTC-12 means no row newer than the watermark is skipped, whatever that zone; up to two days may be read again. A `partition_overwrite` model whose `time_column` is a `DateTime`/`DateTime64` cannot declare `{since:Date}` or `{since:Date32}`: the date cuts a partition at midnight in a zone the run cannot see, so every run after the first would load part of a partition and fail with `PARTITION_INCOMPLETE`, writing nothing (measured, #163). It is refused with `INVALID_MODEL_SQL` before anything runs instead, naming the type to declare. With a `Date`/`Date32` time column that the SQL compares `{since:…}` with, the partition check compares dates, which is exact. An `append` model must declare a `{since:…}` type as precise as its time column (see "Rows read once (`append`)").

How the rows load and how the run ends:
- **Into the run's transaction.** The result streams as Arrow record batches into a table of the run's DuckDB transaction (`Transaction.load_arrow`, `contracts/store/v1`), each column cast to its mapped type. No row is decoded in Python. The model's strategy, tests and watermark then apply as for any other model.
- **Rows read again.** The declared `{since:…}` type may truncate the watermark, so rows between the two are read again. The `partition_overwrite` cut check (`PARTITION_INCOMPLETE`) compares the loaded rows with the table's rows, not with any boundary (#155); `merge` and `delete_insert` replace them by key; an `append` model may not declare a coarser type (#135).
- **Columns checked first.** Before reading, the output is described again, and the stream's Arrow types are compared with the validated columns; a type that changed since validation is `SCHEMA_CHANGED`.
- **Failure and cancellation.** A failure, a time limit or a cancellation shuts the connection down, so ClickHouse cancels the query even while it sends nothing, and the run rolls back. The run record keeps the rows read so far.
- **Ctrl-C.** The run rolls back, its records are written (`failed`, `STORE_CANCELLED`), and the command then stops as interrupted.
- **Lineage.** The model's input dataset is `clickhouse://<host>:<port>`, named `<database>`.

The ClickHouse user needs only `SELECT` on the model's tables; the functions the projection uses need no grant.

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

## Rows read once (`append`)

An `append` model inserts every row it reads, and replaces nothing, so a filter that reads a row again inserts it twice (#135). In an `append` model with `filter: manual`, every `$since` (DuckDB SQL) or `{since:…}` (ClickHouse SQL) must be a **strict lower bound of the time column by the parameter alone**: `time_column > $since` (or `$since < time_column`), and in ClickHouse SQL `time_column > {since:T}`. The bound follows the row-path rules of "Partitions cut by `$since`" below (the query holding it returns the model's rows, joined to its `WHERE` by `AND` only; no `UNION`, join, grouping, window or limit on the path; the time column passed through unchanged), with three differences: `>=` is refused (it reads the rows at the watermark again), no expression may surround the parameter (`$since - INTERVAL 1 HOUR` is a hand-written lookback), and `OR time_column IS NULL` is refused (it reads every undated row in every run).

In ClickHouse SQL, the declared type must also tell apart every instant the time column does, as ClickHouse describes the column at validation: the run truncates the watermark to the declared type, never rounding up, so a coarser type reads again the rows between the truncated value and the watermark.

| `{since:…}` type | Allowed for a time column of type |
|---|---|
| `Date` | none (see below) |
| `Date32` | `Date`, `Date32` |
| `DateTime` | none (see below) |
| `DateTime64(p)` | `DateTime`, `DateTime64(q)` with `p ≥ q` |

**Rows at the default value** (#191, decision #206). A first run binds the type's first value (see "First run, full refresh"), and `>` leaves out the rows equal to it. For `Date` and `DateTime` that first value, `1970-01-01` and `1970-01-01 00:00:00 UTC`, is also ClickHouse's default value, which sources often use for "no date". So an `append` model refuses `{since:Date}` on a `Date` column and `{since:DateTime…}` on a `DateTime` column (`INVALID_MODEL_SQL`, the message naming the type to declare): declare `{since:Date32}` or `{since:DateTime64(0, 'zone')}`, whose first value is earlier, so those rows are loaded. `merge` and `partition_overwrite` may use `>=` and are not affected.

A date column takes a date parameter only: ClickHouse compares a `Date32` with a time at midnight in the connection's zone (the server's, or the user profile's), whatever the parameter's zone, so the watermark's day could be read again.

`Nullable(…)` and `LowCardinality(…)` are read through.

The time column must also have the same value for a row in every run, or a row read once comes back with a later time and is inserted again. So it must be traced from the model's output to table columns, computed on the way only by functions the engine marks as giving the same value in every query (DuckDB's `CONSISTENT` functions, less `current_localtimestamp()`, `current_localtime()`, `version()` and the order-dependent `any_value`, `first`, `last`, `first_value`, `last_value`, `nth_value`; ClickHouse's `deterministic` ones): no window function, no subquery, none of `now()`, `random()`, `current_timestamp`, `now64()`, `rand()` and the like. Anything the check cannot trace is refused:
- **DuckDB SQL** (traced through aliases, stars, `REPLACE`, subqueries, CTEs and joins): column lists that rename by position (`WITH c(a, b)`, `AS q(a, b)`), `RENAME`, `COLUMNS(…)`, positional references (`#2`), struct fields, `unnest`, aggregate functions (`max`, `list`, `arg_max`: a group's value changes as rows join it), table functions (`read_parquet` included), `VALUES`, set operations, three-part names (`feed.data.read_at`);
- **ClickHouse SQL** (traced by name through every alias in the model, so an unrelated alias of the same name is checked too): a tuple's field (`tup.t`, also when the tuple is named like a table or subquery), `* REPLACE (…)`, `COLUMNS(…)`, `ARRAY JOIN`, `untuple`, and table functions other than `numbers` and `zeros`.

The check reads the model's own SQL: a time column the source computes on each read (a `now()` in an upstream view or model, a source view, or a ClickHouse `ALIAS` column) is out of its reach. Anything else is `INVALID_MODEL_SQL` at the model's SQL file before anything runs; the message says what to write (`WHERE <time_column> > $since`, or a `{since:…}` type as precise as the column) and points to `strategy: merge` when rows can be read again. `filter: auto` already writes `time_column > $since`.

**Rows kept** (#183). A row left out of a run, at or before the watermark, is never read again, so the queries the time column's trace passes through (DuckDB SQL), or every query of the model (ClickHouse SQL), must keep every row they read: no `LIMIT`, `OFFSET` or sample (also on a joined table), no `DISTINCT ON`, `QUALIFY` or `LIMIT … BY`, no window function (a query above could choose one row per key with it, as `QUALIFY` does: `row_number() OVER (…) AS rn` … `WHERE rn = 1`), and no subquery that chooses rows by reading others (in the select list, `WHERE`, `HAVING` or a join's `ON`; in ClickHouse SQL any subquery outside `FROM`, such as `IN (SELECT …)`). Plain `DISTINCT`, `GROUP BY` of the time column passed through, inner joins (DuckDB SQL), `WITH x AS (…)` and `ORDER BY` are accepted. In ClickHouse SQL, a table or `WITH` name read in an expression (`x IN ids`) counts as such a subquery. The query that holds the filter itself still reads one table or subquery, not a join (see above). For "the latest row per key", use `merge`.

**Joins** (#189). A join that can give a row another time when other rows arrive makes the row look new, so it is read and inserted again. In DuckDB SQL, the time column's trace may pass only through plain inner joins (`JOIN`, `INNER JOIN`, `CROSS JOIN`, `NATURAL JOIN`): `LEFT`, `RIGHT`, `FULL`, `SEMI`, `ANTI`, `ASOF` and `POSITIONAL` joins on it are refused. In ClickHouse SQL, whose `EXPLAIN AST` does not show a join's kind, an `append` model cannot join at all; load each table with its own model and join them in a DuckDB SQL model.

**A fixed zone** (#189). The watermark is written as local time in the zone of a `{since:DateTime…}` (its declared zone, else the connection's: the server's, or the user profile's `session_timezone`), and ClickHouse reads a local time that repeats when clocks go back as its earlier instant, so up to an hour of rows would be inserted twice. An `append` model's `{since:DateTime…}` must therefore be read in a zone with one UTC offset from 1970 to 2100 (tz database), such as `UTC`, `Etc/GMT+4` or `America/La_Paz`. Zones that change their clocks in those years, such as `America/New_York`, and also `America/Lima` and `America/Bogota` (daylight saving time in the 1990s), are refused, and the message suggests the same type in UTC (`DateTime64(p, 'UTC')`, seconds as `DateTime64(0, 'UTC')`), which compares the same instants. `{since:Date}` and `{since:Date32}` are taken in a fixed zone already.

**Depth.** A model whose queries and expressions nest more than 64 levels deep (a subquery, a function or an operator such as `+` is one level) is refused (`INVALID_MODEL_SQL`, "too deeply nested to check"), for `append` and `partition_overwrite`, rather than checked until a limit is hit. DuckDB and ClickHouse themselves refuse SQL nested beyond their parsers' depth.

Limit: a row that reaches the source later with a time equal to or older than the watermark (late data) is not read by an `append` model under any filter. Use `merge` with a `lookback` for such sources.

## Partitions cut by `$since` (`partition_overwrite`)

A `partition_overwrite` run deletes every table row of each partition it loads, then inserts the loaded rows. With a `$since` filter, a run can load only part of a partition; the delete would then lose the rest. Two rules prevent it (#155, decision #128 option B).

**At validation: `$since` only sets a lower bound of the time column.** In a `partition_overwrite` model with `filter: manual`, every `$since` (DuckDB SQL) or `{since:…}` (ClickHouse SQL) must be in a comparison `time_column > x` or `time_column >= x` (or `x < time_column`, `x <= time_column`):
- the comparison is a condition of the `WHERE` clause (in ClickHouse, of its one condition: `WHERE` or `PREWHERE`, not both) of a query whose rows are the model's rows, joined to it by `AND` only. Such a query is the model's query, or a query it reads whole as its only `FROM` (a subquery, or in DuckDB SQL a CTE read nowhere else, and in a statement without `query(…)` or `query_table(…)`, which read relations by name), and it reads one table or subquery, not a join. A `$since` under a `UNION` is refused: another branch can load an older row of a partition (another table, an older bound, or no bound), which would hide a cut from the run's check. Columns are followed by name, so nothing on the path may rename them by position: no column list on a subquery, table or CTE (`AS t(a, b)`, `WITH c(a, b) AS …`). `OR time_column IS NULL` may follow the comparison, to reload undated rows too;
- every query on that path returns the time column **unchanged** and keeps its rows as they are: the column is in its select list as itself (`day`, `t.day`, `day AS day`, or a plain `*` / `t.*`), no other output is named like it, and the query has no `GROUP BY`, `HAVING` (ClickHouse's `EXPLAIN AST` shows neither `DISTINCT` nor `GROUP BY ALL`, and cannot tell `WHERE` from `HAVING`, `PREWHERE` or `LIMIT`: there a query on the path may have one condition only, a comparison or other condition function such as `and`, `in` or `like` (so not a bare boolean column: write `flag = 1`), before any `ORDER BY`; and validation reads ClickHouse's `EXPLAIN QUERY TREE`, which labels each clause, to refuse any `LIMIT`, `OFFSET` or `LIMIT BY` in the model), window function, `QUALIFY`, `LIMIT`, `OFFSET`, `LIMIT BY`, `DISTINCT ON` or sample (plain `DISTINCT` and `ORDER BY` are allowed). One exception: the query holding the bound may rename or compute the time column when the bound names it without a qualifier and the name means that alias, so the bound applies to the output column:
  - in ClickHouse SQL always (`event_time AS read_at`, `toDate(event_time) AS day … WHERE day >= toStartOfMonth({since:Date})`): ClickHouse reads an unqualified name in `WHERE` as the select list's alias first, and every validation and run query sets `prefer_column_name_to_alias = 0` to keep it so. So no alias named like the time column may be defined anywhere else in the model (`WITH … AS day`, `ORDER BY … AS day`, `(… AS day)` inside a condition): ClickHouse would read the select list's `day` as that alias;
  - in DuckDB SQL only when that query reads one table that has no column of that name (`SELECT *, CAST(read_at AS DATE) AS day FROM feed.data WHERE day >= $since`): DuckDB reads such a name as the table's column first. Validation looks the table's columns up; a CTE or subquery there, or an expression that itself reads a column of that name (`CAST(read_at AS DATE) AS read_at`), is refused;
- `x` uses `$since` and constants but no column and no random value (`date_trunc('month', $since)`, `$since - INTERVAL 3 DAY`, `toStartOfMonth({since:Date})`; not `random()`, `rand()`);
- `time_column` is the column itself: in DuckDB SQL in any letter case, in ClickHouse SQL (whose names are case-sensitive) in its own; a qualifier, if written, names that query's table (its alias or name). In ClickHouse SQL, such a model may not quote a name containing a space, a dot or a parenthesis (``` `fecha lectura` ```): `EXPLAIN AST` prints names as they are, so the rule could not tell where such a name ends.

Anything else is `INVALID_MODEL_SQL` before anything runs. That includes a filter on a finer column (`read_at > $since` with `time_column: day`), a function of the time column (`toStartOfMonth(read_at) >= …`), an upper bound (`< $since + …`, `BETWEEN`), an `OR` or `NOT` around the bound, a bound that reads another column (`greatest($since, read_at)`), a `$since` in an `IN`, `EXISTS` or scalar subquery, in `EXCEPT`/`INTERSECT`, or in a query that joins, a time column computed from the filtered one (`CAST(read_at AS DATE) AS read_at`, `max(read_at) AS read_at`), rows computed over the filtered rows (`GROUP BY`, a window, a CTE read again in a subquery), and a `$since` outside a comparison. Each of these can drop rows in the middle or at the end of a partition, which no check of the loaded rows can see. The rule is read from the parse tree (DuckDB's, or ClickHouse's `EXPLAIN AST`), never from the text. In ClickHouse SQL, `identity(identity(…))` is reserved for that check.

**In each run: the oldest-row rule.** An incremental run with `$since` (not a first run, not a full refresh) checks, inside its transaction and before the delete, each loaded partition (`partition_by` values compared with `IS NOT DISTINCT FROM`, so `NULL` is a partition). It is cut when:

- the table has dated rows of it older than the oldest dated loaded row of it, or the loaded rows of it have no dated row; or
- the table has rows of it with a `NULL` time column, and the loaded rows of it have none (a `$since` filter never reloads those).

Either fails the run with `PARTITION_INCOMPLETE` and nothing is written. With the filter a lower bound (validation), a run loads every row of a partition from some time on, so any partition it loads in part has older rows left in the table, and fails: `> $since`, `>= $since` and a hand-written lookback all do. Load whole partitions instead, for example `WHERE read_at >= date_trunc('month', $since)` for monthly partitions. A model whose SQL does not use `$since` reloads everything it selects and is not checked. The message quotes text values (`'2026-10'`), writes SQL `NULL` as `NULL`, and shortens values longer than 60 characters.

Limits of the check:
- It looks only at what the table and the loaded rows contain, so a source that deleted any of a partition's oldest rows fails it too. `--full-refresh` then rebuilds the table from what the model selects now. If the source keeps only recent data, that also removes every older partition the source no longer has.
- A table row with the same time as the oldest loaded row, but not reloaded, is not detected. With the filter a lower bound, only another condition of the `WHERE` clause can leave such a row out.
- In ClickHouse SQL, a named `WITH` query is not followed (its `EXPLAIN AST` does not show the name): a bound there is refused; write it in a `FROM` subquery instead. A model that joins and filters by `$since` is refused in both languages (#164 tracks following columns through joins).

## Compatibility

New optional fields, new enum values and new error codes are compatible. Renaming or removing a field, or changing a default, is breaking (new `v2`).
