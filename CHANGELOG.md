# Changelog

Product releases of Orvex. Module versions are listed in `versions.toml`; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/) (`docs/process/versioning.md`).

## Unreleased

### Security
- A DuckDB-file source with generated columns is now refused (`STORE_SOURCE_INVALID`), before anything is read (#268). A `VIRTUAL` generated column's expression ran in the lake's session when the column was read, and could return the lake session's settings. `STORED` generated columns are refused too, as a precaution: no leak through them was shown, and DuckDB 2.0.0 cannot create them.

## v0.2.0-alpha.1 — 2026-10-31

Phase P1 (Storage and transformation). Alpha: formats and contracts may still change between phases.

Modules: contracts, server, store and transform `0.2.0`; access, dashboards, mcp and semantic stay `0.1.0`.

### Added
- `store`: Add the store contract v1 and the DuckLake adapter with a local catalog (#72)
- `store`: Add the DuckLake adapter with a PostgreSQL catalog (#74)
- `store`: Map DuckDB types exactly to Arrow and JSON (#75)
- `store`: Add query budgets and admission control (#76)
- `server`: Load project.yaml and connections.yaml (project contract v1) (#78)
- `store`: Attach Parquet, CSV and DuckDB file sources read-only (#81)
- `store`: Attach SQLite and PostgreSQL sources read-only (#83)
- `transform`: Add the model-config contract v1 and model discovery (#85)
- `transform`: Add the dependency graph, materializations and validate/run (#87)
- `transform`: Add the run-record contract v1 and run history (#89)
- `transform`: Run incremental models with append and merge (#91)
- `transform`: Add delete_insert, partition_overwrite and schema-change policies (#93)
- `transform`: Support the manual $since filter, watermark and lookback (#94)
- `transform`: Run model tests with write-audit-publish (#97)
- `transform`: Reconcile models against their sources (#100)
- `transform`: Add the freshness-driven scheduler (#104)
- `transform`: Inject the incremental filter automatically when provable (#106)
- `transform`: Emit OpenLineage events (#102)
- `transform`: Connect to ClickHouse and validate extraction models (#111)
- `store`: Expire old snapshots and compact files as maintenance runs (#115)
- `transform`: Fail partition_overwrite runs whose since filter cuts a partition (#125)
- `transform`: Run ClickHouse extraction models into the lake (#129)
- `store`: Choose the lake catalog (DuckDB file, SQLite or PostgreSQL with verified TLS) (#132)
- `server`: Make the scheduler fit to run unattended (#165)
- `store`: Declare the lake's data-file location, S3-compatible storage and encryption (#136)
- `transform`: The partition cut check uses the oldest-row rule (#162)
- `store`: Data files in Google Cloud Storage and Azure Blob Storage (#171)
- `tools`: Place the load baseline's memory peak and record where it goes (#186)

### Changed
- `repo`: Add a ClickHouse service for the extraction-model tests (#110)
- `repo`: Auto-merge tier C Dependabot updates (#148)
- `web`: Ship the licenses of the code bundled into the web build (#150)
- `server`: Add the load-performance baseline workflow and script (#167)
- `repo`: Close the P1 exit-checklist gaps in error documentation and CI evidence (#174)
- `transform`: Measure a date since on a zoned partition_overwrite column without its refusal (#215)
- `transform`: Retire the ClickHouse extraction models (ADR-0028) (#227)
- Split the Python tests into 10 parallel parts per version (#262)

### Fixed
- `store`: Map DuckDB token byte offsets to character offsets (#131)
- `server`: Answer an unsupported method with METHOD_NOT_ALLOWED (#138)
- `server`: Delete the in-memory project on SIGTERM and SIGHUP (#145)
- `server`: Answer only requests addressed to the local server (#142)
- `repo`: Catch every way of reading rows into Python (#149)
- `server`: Stop gracefully on Ctrl-C, as on SIGTERM (#152)
- `transform`: Require verified TLS for remote PostgreSQL sources (#134)
- `store`: No file name through dollar quotes, no SUMMARIZE/DESCRIBE/SHOW (#154)
- `store`: Hold admission, time limits and cancellation at their edges (#159)
- `server`: Name loader problems where they are and treat an empty connections.yaml as none (#161)
- `store`: Make the statement checks linear and parse SQL before them (#172)
- `server`: Bound --stop-grace and show scheduler problems in the heartbeat (#177)
- `server`: Refuse a model whose output repeats a column name (#176)
- `store`: Bound the store's own catalog queries by the request's limit (#180)
- `transform`: An append model's filter reads each row once (#175)
- `server`: Record a lake before its first open and confirm it after (#181)
- `store`: Bound the cost of E-strings and file patterns in the statement checks (#184)
- `transform`: An append model keeps every row it reads, and deep nesting is refused (#188)
- `transform`: Refuse append joins that can change a row's time, and zones whose clock goes back (#199)
- `transform`: Refuse a {since:…} that starts at 1970 for a time column from 1900 (#187)
- `server`: Refuse a lake whose catalog file is missing, and report record write errors (#202)
- `ci`: Write the wheel-contents check of release.yml as a block scalar (#198)
- `store`: Read each folder once per statement in the file-pattern check (#200)
- `server`: Accept --port from 1 to 65535 only (#209)
- `transform`: Read the ClickHouse connection's zone from the server, never a silent UTC (#203)
- `transform`: An append model's first run reads the rows at its column's default value (#208)
- `server`: Record the default lake and refuse a lost catalog instead of recreating it (#207)
- `transform`: Accept only IANA zone names for {since:…}, read from the bundled database (#216)
- `store`: Read Unicode spaces exactly as DuckDB's parser prepass does before checking a statement (#214)
- `store`: Record the storage endpoint, refuse AT time travel, keep data paths folders (#211)
- `store`: Refuse source files whose names DuckDB's readers would expand again (#229)
- `transform`: An append model's rows pass through no join or random filter (#230)
- `server`: Leftover tables, schema-qualified view columns, run output and lock edge cases (#234)
- `store`: Serve file sources exactly or refuse the column (#253)

### Documentation and process
- `repo`: Adopt the engineering charter (#116)
- `adr`: Two data routes converging on the semantic engine (ADR-0027) (#121)
- `repo`: Move the server-supervised worker from P1-21 to P3-15 (#157)
- `server`: Record the P1 load-performance baseline (#169)
- `architecture`: Point the ClickHouse first-run value of $since to the contract (#196)
- `adr`: ClickHouse by Direct Query only (ADR-0028); raw sources DuckDB-native (#223)

## v0.1.0-alpha.1 — 2026-10-27

Phase P0 (Foundation). Alpha: formats and contracts may still change between phases.

Modules, all `0.1.0`: access, contracts, dashboards, mcp, semantic, server, store, transform.
Contracts: `http-api` v1.

### Added
- Repository governance: governed paths with the `governance` check, status labels, Dependabot (#15).
- Python workspace: one distribution per module, exact pins, lock files for Python 3.15 and 3.14, ruff, mypy strict, pytest (#20).
- Web workspace: SvelteKit, Svelte 5, TypeScript strict, Tailwind CSS 4, license allow-list (#22).
- Identity loader, one-step rename script, product-name and module-boundary checks (#24).
- Contracts workspace: JSON Schema and OpenAPI validation, generated Python and TypeScript types (#27).
- `http-api` contract v1 (health, version, error envelope) and the typed web client (#29).
- Server: `orvex` command, local mode on 127.0.0.1, project folders, JSON logs with request id (#31).
- Synthetic electricity dataset: consumers, meters, readings, invoices, payments and credit notes, byte-reproducible from a seed (#32, #34).
- Semantic corpus: bi-platform's 21 normative cases imported unchanged, Apache Ossie schema pinned, corpus runner reporting `not-run` (#35).
- Version manifest, contract compatibility check at startup, release workflow, Docker skeleton (#37).

### Changed
- The product is named Orvex (previously Sextant); `SEXTANT` stays accepted as a previous Ossie vendor name (#39).

### Fixed
- The server wheel declares its dependency on the contracts wheel; release tags must match `versions.toml` (#40).
