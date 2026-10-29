# `store` contract v1

**Producer:** `store` (`orvex.store`). **Consumers:** `transform` (writes, from P1), `semantic` (reads, from P2), `server`.

How every other module reads and writes analytical data in the lake (DuckLake), without knowing DuckDB.

## Messages (`schema.json`)

| Message | Purpose |
|---|---|
| `LakeLocation` | Where the lake is: `LocalLakeLocation` (`catalog: local`, a DuckDB-file catalog at `<path>/catalog.ducklake` or at `catalog_path`, data files under `<path>/data/`, one process), `SqliteLakeLocation` (`catalog: sqlite`, see below) or `PostgresLakeLocation` (`catalog: postgres`, see below) |
| `StatementRequest` | One SQL statement run inside a write transaction (`CREATE TABLE … AS`, `INSERT … SELECT`, `MERGE`, `UPDATE`, `DELETE`, `DROP`, `ALTER`) |
| `ReadRequest` | One `SELECT` read **once**, as of `snapshot`, in batches of `batch_size` rows |
| `Parameter` | A value bound to `$name`; `value` is text (or `null`) converted to the declared DuckDB type, so exact values never travel as JSON numbers. Values are never concatenated into SQL. `DATE`, `TIMESTAMP` and `TIMESTAMPTZ` values are ISO 8601 (`YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[.ffffff]`, the same with `Z` or `±HH:MM`), or `"infinity"` / `"-infinity"` |
| `Snapshot` | A lake snapshot: `id` and `committed_at` (with offset) |
| `CommitResult` | The one snapshot a committed transaction created, or `null` when it changed nothing |
| `MaintenanceReport` | What one `maintain` call did (below) |
| `StoreError` | `code`, `message`, `details` |
| `ResultJson` | A read result as JSON: `columns` (`name`, DuckDB `type`) and positional `rows`, encoded exactly (below) |
| `SourceAttachment` | A source given to `open_lake` — files (`parquet`, `csv`, `duckdb`) or databases (`sqlite`, `postgres`); readable by name inside write transactions only (below) |

## File sources (`SourceAttachment`)

`open_lake(location, limits, sources)` makes each source a read-only catalog named after its connection, before the file sandbox is set:

| `type` | Tables |
|---|---|
| `parquet`, `csv` | one table `<name>.data`: every file matching `path` (a glob is allowed), columns united by name, types inferred by DuckDB |
| `duckdb` | the file attached `READ_ONLY`: `<name>.<schema>.<table>`, and `<name>.<table>` for schema `main` |

- Only **write transactions** read sources; a read (`ReadRequest`) still sees only the lake (`STORE_STATEMENT_INVALID`).
- The file sandbox gains exactly the declared files: for a glob, the files it matches when the lake opens (a file added later, or any other file in the same folder, stays unreadable).
- Statements still cannot call file readers (`read_parquet`, `read_csv`, …), and a quoted string or name that names an existing file, or that is an object-storage address (`s3://`, `s3a://`, `s3n://`, `gs://`, `gcs://`, `r2://`, `az://`, `azure://`, `abfs://`, `abfss://`, `hf://`, any case), is refused wherever it appears (`FROM '…'`, `MERGE … USING '…'`, `PIVOT '…'`, …), in every quoting form (`'…'`, `E'…'`, `$$…$$`, `$tag$…$tag$` with any tag, `"…"`): a model reaches a file only through its connection (`STORE_STATEMENT_INVALID`). A single string constant starting with one of those addresses is therefore refused; web addresses (`https://…`) are not. (Text built from several constants is not checked; no place where DuckDB opens a file accepts such an expression.)
- `SUMMARIZE`, `DESCRIBE` (`DESC`) and `SHOW` are refused wherever DuckDB could read them as a statement: at the start, in a subquery, after a set operator, as an `INSERT` source or after `CREATE … AS` (`STORE_STATEMENT_INVALID`). DuckDB reads a quoted `SUMMARIZE` target as a file, and these reads would not be pinned to their snapshot. The words remain allowed as a column label after `AS` (`SELECT a AS desc`), as part of a qualified name (`t.desc`), and `DESC` as an ordering in `ORDER BY`. Quoted names (`"describe"`) are ordinary names. A read is also refused when DuckDB's parse tree contains such a statement.
- A DuckDB source that defines views or macros is refused (`STORE_SOURCE_INVALID`): they would run with the lake's connection.
- Nothing can be written to a source; a DuckDB source file is never modified.
- A path that matches no file → `SOURCE_NOT_FOUND`, naming the source, when the lake opens. Two sources with the same name → `STORE_SOURCE_INVALID`.

## Database sources (`SqliteSourceAttachment`, `PostgresSourceAttachment`)

| `type` | Attached as | Tables |
|---|---|---|
| `sqlite` | a private copy taken at open, attached with DuckDB's `sqlite` extension, `READ_ONLY` (a missing file is `SOURCE_NOT_FOUND`, never created) | `<name>.<table>` |
| `postgres` | DuckDB `postgres` extension, `READ_ONLY`, and the session set `default_transaction_read_only = on`; password from `password_env` | `<name>.<table>` with `schema`, otherwise `<name>.<schema>.<table>` |

- Nothing can be written to a database source, even when its user could write.
- A SQLite source is copied when the lake opens (SQLite's backup, WAL included) and the copy is attached: later changes to the file do not reach the lake until it is opened again. A SQLite file that defines views is refused (`STORE_SOURCE_INVALID`): DuckDB would run a view's SQL with the lake's connection.
- Functions that run SQL on a source (`postgres_query`, `postgres_execute`, `sqlite_query`, …) and `ATTACH`/`DETACH` are refused in statements (`STORE_STATEMENT_INVALID`).
- **Exact types (law 11):** a column DuckDB cannot carry exactly — PostgreSQL `numeric` without precision, with more than 38 digits or with a negative scale or one above its precision, `money` and arrays of either, in tables, views and materialized views; SQLite columns whose declared type contains `DEC` or `NUM` (`DECIMAL`, `NUMERIC`, `NUMBER`; DuckDB reads them as floating point, and SQLite stores them so) — is refused when a write reads it: `UNSUPPORTED_SOURCE_TYPE` naming `<source>.<table>.<column>`. Columns a write does not read do not matter.
- **TLS (#122).** A PostgreSQL source on a host other than the local machine must have `sslmode: verify-full`. The local machine is decided as for a catalog: the host is `localhost`, `127.0.0.1` or `::1`, `PGHOSTADDR` (when set) is local too, and no `PGSERVICE` is set, since libpq reads both from the environment; otherwise it is refused before anything is opened (`STORE_TLS_REQUIRED`). `sslrootcert` is `system` (the default) or `${env:NAME}`, a variable holding a CA file's path, read by the store. `sslmode: disable` is for the local machine only.
- A connection or credential failure is `STORE_CONNECTION_FAILED` naming the source, with the password redacted; an unset variable (password or CA) is `STORE_SECRET_MISSING`.

## File catalogs (`LocalLakeLocation.catalog_path`, `SqliteLakeLocation`)

- `LocalLakeLocation.catalog_path`, when given, is the DuckDB catalog file instead of `<path>/catalog.ducklake`; data files stay under `<path>/data/`. The store adds exactly that file and its `.wal` to the file sandbox; DuckDB adds the `.wal.checkpoint` and `.wal.recovery` files of each database it attaches.
- `SqliteLakeLocation` keeps the catalog in the SQLite file `path` (DuckDB's official `sqlite` extension), so several processes on one machine can use the lake; data files go under `data_path`. The store adds exactly the catalog file and its `-wal`, `-shm` and `-journal` files to the file sandbox, no folder.
- A file catalog must be on a local disk; the server's loader checks that before it builds the location (`contracts/project/v1`).

## Data files (`data_path`, `storage`, `encrypted`, #126)

- `data_path` is where data files are written: `<path>/data/` by default for `LocalLakeLocation`, otherwise an absolute local folder or `s3://bucket/prefix/`.
- An `s3://` path needs `storage` (type `s3`: `region`, `use_ssl`, optional `endpoint` and `url_style`, and the names of the environment variables holding `key_id` and `secret`). `storage` needs an `s3://` path. Any other URL is refused (`STORE_STATEMENT_INVALID`).
- The credentials are read when the lake opens and registered so no error shows them. They become a **temporary** DuckDB secret (official `httpfs` extension) scoped to the prefix; persistent secrets are never loaded. An unset variable is `STORE_SECRET_MISSING`.
- `use_ssl: false` is refused unless the endpoint is the local machine (`STORE_TLS_REQUIRED`), before anything is opened.
- The file sandbox allows exactly the data prefix. DuckDB checks remote paths against it too: another prefix of the same bucket stays closed.
- **Known limit (DuckDB 2.0.0, decision #140).** DuckDB checks an `s3://` path against the sandbox as text and resolves `..` afterwards, so `s3://bucket/prefix/../other` passes the check and reaches outside the prefix. No statement can name a file or an object-storage address (`STORE_STATEMENT_INVALID`), so only DuckLake's own paths reach object storage. Give the lake credentials that grant access to its prefix only.
- `encrypted: true` attaches with DuckLake's `ENCRYPTED`; the keys are stored in the catalog. Deletion vectors (`WRITE_DELETION_VECTORS`, written unencrypted) are never enabled.
- Maintenance (expiration, merging, cleanup and orphan deletion) works the same on object storage. Orphan deletion removes every `.parquet` file under the data path that the catalog does not list, so the data folder or prefix must be **dedicated to the lake**.

## PostgreSQL catalog (`PostgresLakeLocation`)

Several processes read and write one lake. `connection` holds libpq `key=value` pairs **without** a password (any `password` key or URI form is refused with `STORE_SECRET_INLINE`); the password is read at open time from the environment variable named in `password_env` and never appears in errors. `metadata_schema` keeps the catalog tables of one lake in their own PostgreSQL schema (default `public`), so one database can hold several lakes. `data_path` is where data files are written.

**TLS.** A catalog on a host other than the local machine must be reached with `sslmode=verify-full`, which encrypts the connection and checks that the server's certificate belongs to the named host; any other mode is refused before anything is opened (`STORE_TLS_REQUIRED`). Local means every host libpq could use is `localhost`, `127.0.0.1`, `::1` or a Unix-socket directory (a host starting with `/`), including `hostaddr`; `PGHOST`/`PGHOSTADDR` count when the connection names no host, and a service (`service=`, `PGSERVICE`) is never local. With `verify-full`, the operating system's authorities are used (`sslrootcert=system`) unless the connection names a CA file. The store is the enforcement point, so every user of this contract gets the rule.

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

`ReadRequest` and `StatementRequest` accept `timeout_ms` (1–3,600,000; default set by the lake). A lake runs a bounded number of statements at once (reads until their stream ends, transactions until they commit or roll back); further requests wait in arrival order in a bounded queue, and are refused with `STORE_BUSY` when it is full. Waiting counts against the timeout. A statement past its time limit is interrupted (`STORE_TIMEOUT`); a read or a transaction can be cancelled from another thread (`STORE_CANCELLED`); an interrupted or cancelled transaction rolls back. Ctrl-C during a statement of a transaction (`execute`, `value`, `load_arrow`) raises Python's `KeyboardInterrupt`, as in any Python program, and the transaction rolls back.

## Arrow loads (`Transaction.load_arrow`)

`load_arrow(schema, table, reader, columns, on_interrupt=None, timeout_ms=None)` creates `schema.table` in the lake, inside the write transaction, from an Arrow record-batch stream (P1-17, #98):
- `columns` are `(name, DuckDB type)` pairs. The stream's fields must be exactly those names, in order, and no two names may differ only in letter case (`STORE_STATEMENT_INVALID` otherwise).
- Each column is cast to its type (`CREATE TABLE … AS SELECT CAST(…) …`). The stream is read once, batch by batch, by DuckDB, and no row passes through Python.
- It returns the number of rows.
- A type name must be a DuckDB type name with optional numeric arguments (`DECIMAL(18,4)`); anything else is refused before the SQL is built.
- Time limits and cancellation apply as for statements. `on_interrupt` is called from another thread when the statement times out or is cancelled, and on Ctrl-C when the load runs in the main thread, so the caller can close the stream's source (a remote query then stops, even while it sends nothing). Ctrl-C then raises `KeyboardInterrupt`.
- An exception raised by the stream itself propagates unchanged; after a time limit or a cancellation, the store's error is raised instead, even if the statement ended.
- The transaction rolls back on any failure.

Like a Python method, this is part of the contract's Python protocol; it has no JSON message.

## Maintenance (`MaintenanceReport`)

`Lake.maintain(expire_before, orphans_before)` takes two aware instants. It waits for every read and write of the lake to end and runs alone: it takes every admission slot, then the writer lock. It follows DuckLake's own `CHECKPOINT` order, step by step, so that each step is counted:

1. flush inlined data;
2. expire the snapshots committed before `expire_before`;
3. merge adjacent small files;
4. rewrite files with many deleted rows;
5. expire again (the snapshot that merging replaced, when it is older than the cutoff);
6. delete the files no remaining snapshot uses;
7. delete orphaned files in the data folder that are older than `orphans_before` (a newer one may belong to a writer that has not committed yet).

The current snapshot is never expired. Merging never changes what any kept snapshot reads. The report counts:
- `snapshots_expired`;
- `files_merged` and `files_rewritten`, the files processed by steps 3 and 4;
- `files_created`, the files those steps wrote;
- `files_deleted`;
- `orphaned_files_deleted`;
- `oldest_snapshot`, the oldest snapshot kept.

A read pinned to a snapshot that maintenance expired fails with `STORE_SNAPSHOT_EXPIRED`, which names the oldest one kept. Snapshot ids are never reused and every id up to the current one was committed, so a missing id in that range expired. Any other id is `STORE_SNAPSHOT_NOT_FOUND`.

Preconditions:
- **No other process uses the lake.** In this process, `maintain` excludes reads and writes itself. Other processes are excluded by the caller; the server holds the project lock.
- **One data path per lake.** A data folder shared by two lakes would make each delete the other's files as orphans.
- **Partial work.** If a step fails, `maintain` raises, and the steps already done (expired snapshots, merged or deleted files) stay done. None of them changes what a kept snapshot reads.

## Guarantees

- A write transaction commits to exactly one snapshot, or rolls back and creates none.
- A read sees exactly the data of its `snapshot`, whatever is committed afterwards.
- A read's SQL may reference only lake tables (and table functions such as `range`); it may not name another catalog.

## Error codes

| Code | Meaning |
|---|---|
| `STORE_SNAPSHOT_NOT_FOUND` | The requested snapshot does not exist |
| `STORE_SNAPSHOT_EXPIRED` | The requested snapshot existed but was expired by maintenance (a missing id up to the current one) |
| `STORE_STATEMENT_INVALID` | Not exactly one statement, a non-`SELECT` in a read, a statement kind not allowed in a transaction, or a read naming another catalog |
| `STORE_QUERY_FAILED` | DuckDB rejected or failed the statement; its message is in `details` |
| `STORE_PARAMETER_INVALID` | A parameter is missing, extra, or not convertible to its declared type |
| `STORE_LIMITS_REQUIRED` | The lake was opened without an explicit memory limit and thread count |
| `STORE_EXTENSION_UNAVAILABLE` | A DuckDB extension the lake needs could not be installed or loaded |
| `STORE_TRANSACTION_CONFLICT` | Another writer committed a conflicting change first; nothing of this transaction was written |
| `STORE_SECRET_MISSING` | An environment variable the location names is not set: `password_env`, or the object storage's `key_id_env` or `secret_env` (the variable is named, never a value) |
| `STORE_SECRET_INLINE` | The PostgreSQL `connection` contains a password or is a URI |
| `STORE_TIMEOUT` | The statement exceeded its time limit (waiting included); a transaction rolled back |
| `STORE_CANCELLED` | The read or transaction was cancelled; a transaction rolled back |
| `STORE_TLS_REQUIRED` | A PostgreSQL catalog or source on another machine without `sslmode=verify-full`, or object storage on another machine without HTTPS (`use_ssl: false`); `details` names the host or endpoint and the mode found (and the source) |
| `STORE_BUSY` | The lake is running its maximum number of statements and its queue is full |
| `STORE_TYPE_UNSUPPORTED` | A result column has a type with no JSON encoding yet |
| `STORE_CATALOG_UNAVAILABLE` | The lake cannot be opened: its catalog (a DuckDB or SQLite file, or PostgreSQL) cannot be reached or refused the credentials, its data files cannot be reached, or DuckDB refused the object storage settings; DuckDB's message is in `details`, never a credential |
| `STORE_LOCATION_INVALID` | The data location is not a local path or an `s3://` prefix, or an `s3://` path and the storage settings do not come together (new in #126, compatible) |

## Compatibility

New optional fields, new `LakeLocation` catalogs and new error codes documented as such are compatible changes within v1.
