# `store` contract v1

**Producer:** `store` (`orvex.store`). **Consumers:** `transform` (writes, from P1), `semantic` (reads, from P2), `server`.

How every other module reads and writes analytical data in the lake (DuckLake), without knowing DuckDB.

## Messages (`schema.json`)

| Message | Purpose |
|---|---|
| `LakeLocation` | Where the lake is: `LocalLakeLocation` (`catalog: local`, a DuckDB-file catalog at `<path>/catalog.ducklake`, data files under `<path>/data/`, one process) or `PostgresLakeLocation` (`catalog: postgres`, see below) |
| `StatementRequest` | One SQL statement run inside a write transaction (`CREATE TABLE … AS`, `INSERT … SELECT`, `MERGE`, `UPDATE`, `DELETE`, `DROP`, `ALTER`) |
| `ReadRequest` | One `SELECT` read **once**, as of `snapshot`, in batches of `batch_size` rows |
| `Parameter` | A value bound to `$name`; `value` is text (or `null`) converted to the declared DuckDB type, so exact values never travel as JSON numbers. Values are never concatenated into SQL |
| `Snapshot` | A lake snapshot: `id` and `committed_at` (with offset) |
| `CommitResult` | The one snapshot a committed transaction created, or `null` when it changed nothing |
| `StoreError` | `code`, `message`, `details` |
| `ResultJson` | A read result as JSON: `columns` (`name`, DuckDB `type`) and positional `rows`, encoded exactly (below) |
| `SourceAttachment` | A file source (`parquet`, `csv` or `duckdb`) given to `open_lake`; readable by name inside write transactions only (below) |

## File sources (`SourceAttachment`)

`open_lake(location, limits, sources)` makes each source a read-only catalog named after its connection, before the file sandbox is set:

| `type` | Tables |
|---|---|
| `parquet`, `csv` | one table `<name>.data`: every file matching `path` (a glob is allowed), columns united by name, types inferred by DuckDB |
| `duckdb` | the file attached `READ_ONLY`: `<name>.<schema>.<table>`, and `<name>.<table>` for schema `main` |

- Only **write transactions** read sources; a read (`ReadRequest`) still sees only the lake (`STORE_STATEMENT_INVALID`).
- The file sandbox gains exactly the declared files (or, for a glob, the folder before its first wildcard), never their parents.
- Statements still cannot call file readers (`read_parquet`, `read_csv`, …) or name a file in `FROM '…'`: a model reaches a file only through its connection (`STORE_STATEMENT_INVALID`).
- Nothing can be written to a source; a DuckDB source file is never modified.
- A path that matches no file → `SOURCE_NOT_FOUND`, naming the source, when the lake opens. Two sources with the same name → `STORE_SOURCE_INVALID`.

## PostgreSQL catalog (`PostgresLakeLocation`)

Several processes read and write one lake. `connection` holds libpq `key=value` pairs **without** a password (any `password` key or URI form is refused with `STORE_SECRET_INLINE`); the password is read at open time from the environment variable named in `password_env` and never appears in errors. `metadata_schema` keeps the catalog tables of one lake in their own PostgreSQL schema (default `public`), so one database can hold several lakes. `data_path` is where data files are written.

Statements never reference the catalog database itself, nor call functions that run SQL elsewhere (`postgres_query`, `postgres_execute`, `query`, …): `STORE_STATEMENT_INVALID`.

## Read results: Arrow

A read returns an Arrow record-batch stream. Arrow types follow DuckDB types exactly:

| DuckDB | Arrow |
|---|---|
| `DECIMAL(p,s)` | `decimal128(p,s)` |
| `HUGEINT` | `decimal128(38,0)` |
| `UBIGINT` | `uint64` |
| `BIGINT` | `int64` |
| `TIMESTAMPTZ` | `timestamp[us, tz=UTC]` |
| `NULL` (any type) | Arrow null |
| `UHUGEINT` | `utf8` (decimal text; Arrow has no exact unsigned 128-bit type) |

Each Arrow field carries its DuckDB type in the field metadata key `duckdb_type`.

No value passes through a floating-point type unless its DuckDB type is `FLOAT` or `DOUBLE`.

## JSON encoding (`ResultJson`)

Every JSON response that carries data (HTTP API, MCP, exports) uses this encoding.

| DuckDB type | JSON |
|---|---|
| `BOOLEAN` | `true` / `false` |
| `TINYINT`, `SMALLINT`, `INTEGER`, `UTINYINT`, `USMALLINT`, `UINTEGER` | number |
| `BIGINT`, `UBIGINT`, `HUGEINT`, `UHUGEINT` | string of digits, optional leading `-` |
| `DECIMAL(p,s)` | string in plain notation with exactly `s` fraction digits (`"12.50"`, `"0.0000001000"`); negative zero is `"0.00"` |
| `FLOAT`, `DOUBLE` | number (for `FLOAT`, the shortest decimal that reads back as the same 32-bit value); `"NaN"`, `"Infinity"`, `"-Infinity"` as strings |
| `VARCHAR`, `UUID` | string |
| `DATE` | `YYYY-MM-DD`; years outside 1–9999 use astronomical numbering with a sign and more digits (`-0044-03-15`, `29349-01-26`); `"infinity"` / `"-infinity"` |
| `TIMESTAMP` | `YYYY-MM-DDTHH:MM:SS.ffffff` (no offset); same year rules; `"infinity"` / `"-infinity"` |
| `TIMESTAMP WITH TIME ZONE` | `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC); same year rules; `"infinity"` / `"-infinity"` |
| `INTERVAL` | ISO 8601 duration `P<months>M<days>DT<seconds>S` (each part may be negative) |
| `BLOB` | base64 |
| any, `NULL` | `null` |

Other types (lists, structs, maps, unions, `TIME`, geometry, …) are refused with `STORE_TYPE_UNSUPPORTED` before any row is produced.

## Time limits, cancellation and admission

`ReadRequest` and `StatementRequest` accept `timeout_ms` (1–3,600,000; default set by the lake). A lake runs a bounded number of statements at once (reads until their stream ends, transactions until they commit or roll back); further requests wait in arrival order in a bounded queue, and are refused with `STORE_BUSY` when it is full. Waiting counts against the timeout. A statement past its time limit is interrupted (`STORE_TIMEOUT`); a read or a transaction can be cancelled from another thread (`STORE_CANCELLED`); an interrupted or cancelled transaction rolls back.

## Guarantees

- A write transaction commits to exactly one snapshot, or rolls back and creates none.
- A read sees exactly the data of its `snapshot`, whatever is committed afterwards.
- A read's SQL may reference only lake tables (and table functions such as `range`); it may not name another catalog.

## Error codes

| Code | Meaning |
|---|---|
| `STORE_SNAPSHOT_NOT_FOUND` | The requested snapshot does not exist |
| `STORE_STATEMENT_INVALID` | Not exactly one statement, a non-`SELECT` in a read, a statement kind not allowed in a transaction, or a read naming another catalog |
| `STORE_QUERY_FAILED` | DuckDB rejected or failed the statement; its message is in `details` |
| `STORE_PARAMETER_INVALID` | A parameter is missing, extra, or not convertible to its declared type |
| `STORE_LIMITS_REQUIRED` | The lake was opened without an explicit memory limit and thread count |
| `STORE_EXTENSION_UNAVAILABLE` | A DuckDB extension the lake needs could not be installed or loaded |
| `STORE_TRANSACTION_CONFLICT` | Another writer committed a conflicting change first; nothing of this transaction was written |
| `STORE_SECRET_MISSING` | The environment variable named in `password_env` is not set (the variable is named, never a value) |
| `STORE_SECRET_INLINE` | The PostgreSQL `connection` contains a password or is a URI |
| `STORE_TIMEOUT` | The statement exceeded its time limit (waiting included); a transaction rolled back |
| `STORE_CANCELLED` | The read or transaction was cancelled; a transaction rolled back |
| `STORE_BUSY` | The lake is running its maximum number of statements and its queue is full |
| `STORE_TYPE_UNSUPPORTED` | A result column has a type with no JSON encoding yet |
| `STORE_CATALOG_UNAVAILABLE` | The PostgreSQL catalog cannot be reached or refused the credentials |

## Compatibility

New optional fields, new `LakeLocation` catalogs and new error codes documented as such are compatible changes within v1.
