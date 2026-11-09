# Changelog

Product releases of Orvex. Module versions are listed in `versions.toml`; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/) (`docs/process/versioning.md`).

## v0.3.0-alpha.1 — 2026-11-09

Phase P2 (Semantic engine: parity with SEF). Alpha: formats and contracts may still change between phases.

**Requires Python 3.14 or later.**

Modules: contracts, server and store `0.3.0`; access and semantic `0.2.0`; transform `0.2.1`; dashboards and mcp stay `0.1.0`.

### Added
- `contracts`: Ossie-extensions v1, the payload of every v1 semantic declaration (#281)
- `semantic`: Semantic-error v1 and the stable error catalog (P2-02) (#282)
- `semantic`: Ossie adapter, read and validate documents into a reference-resolved OssieModel (P2-03) (#286)
- `semantic`: Canonical JSON (own RFC 8785) and fingerprint primitives (P2-04a) (#292)
- `semantic`: Expression parser, P2-03 advisories, cycles and lineage (P2-04b) (#294)
- `semantic`: Type system, exact decimal rules and source catalog (P2-04c) (#295)
- `semantic`: Units and currencies by dimensional analysis (P2-04d) (#296)
- `semantic`: Required declarations, CompiledModel and model fingerprints (P2-04e) (#298)
- `semantic`: Semantic-query v1 and QUERY_INVALID (P2-05a) (#299)
- `semantic`: Bind semantic-query v1 to a canonical form and fingerprint (P2-05b) (#300)
- `semantic`: Semantic-result v1 and PAGE_CURSOR_INVALID (P2-06a) (#301)
- `semantic`: Semantic results, reference reader and cursor contract (P2-06b) (#302)
- `semantic`: Receipt v1, the record of every result and its views (P2-07) (#305)
- `semantic`: Security-policies v1, security.yaml and the policy model (P2-08) (#307)
- `access`: Security-context v1, the permission fingerprint and a fixture provider (P2-09) (#308)
- `semantic`: Catalog v1, the members a caller may see, and Ossie export with a loss report (P2-10) (#312)
- `semantic`: Correctness engine framework and C1, the grain algebra (P2-11) (#323)
- `semantic`: Planner, DuckDB adapter and pinned execution; first real corpus results (P2-12) (#324)
- `semantic`: Ratios recomputed from components with exact division and declared rounding (P2-13) (#328)
- `semantic`: Distinct counts and period-end balances (P2-14) (#330)
- `semantic`: Relationship paths and roles, completing C4 (P2-15) (#332)
- `semantic`: Isolate each fact and align on the requested grain (P2-16) (#335)
- `semantic`: Align facts on conformed dimensions and compute metrics over metrics last (P2-17) (#336)
- `semantic`: Allocate a metric through a bridge with weights validated in the same read (P2-18) (#338)
- `semantic`: Convert instants to their declared zone before calendar bucketing (P2-19) (#339)
- `semantic`: Guard as-of joins to exactly one match, and run totals after aggregation (P2-20) (#342)
- `semantic`: Place row policies before aggregation at every read (P2-21) (#343)
- `semantic`: Verify obligation discharge on the plan and the statement (P2-22) (#347)
- `semantic`: Totals and subtotals through grouping sets, with every rule at every level (P2-23) (#349)
- `semantic`: Detail pages at the base grain, in keyset pages on one snapshot (P2-24) (#350)
- `semantic`: Materialization equivalence proof and substitution, with exact policy preservation (P2-25) (#353)
- `semantic`: Evidence checked per snapshot and relied on by plans (P2-26) (#356)
- `store`: Snapshot file statistics for read-cost upper bounds (P2-24b) (#357)
- `server`: Load and compile the project's semantic model at an exact snapshot (P2-26a) (#360)
- `server`: Verify evidence after runs and on demand, kept in the lake (P2-26b) (#362)
- `server`: HTTP semantic query and catalog behind the serve session's credentials (P2-27a) (#365)
- `server`: Explain a semantic question with no data read; query performance baseline (P2-27b) (#366)
- `semantic`: Add the Direct Query foundation (P2-29) (#371)
- `store`: Read every database source in a worker without the lake (#372) (#374)
- `semantic`: Apply propagated row policies to an aggregate table that keeps their link (#384)
- `server`: Say new rows and the table total, and when the scheduler runs next (#396)
- `store`, `server`: ClickHouse Direct Query connection — verified read-only, exact reads (#225, PR 1 of 4) (#400)
- `store`: Attach an existing DuckLake read-only and isolated (#399, PR 1 of 3) (#405)
- `store`, `server`: Keep an existing DuckLake apart from the instance's files (#399, PR 2 of 3) (#407)
- `semantic`: Add the ClickHouse dialect adapter, core capabilities (#225, PR 2 of 4) (#408)
- `semantic`, `store`, `server`: Wire an existing DuckLake as a Direct Query engine (#399, PR 3a) (#413)
- `server`, `store`, `conformance`: Serve existing DuckLakes and prove corpus parity on them (#399, PR 3b) (#416)
- `semantic`: Run every capability on the ClickHouse adapter (#225, PR 3) (#418)
- `semantic`, `server`, `conformance`: ClickHouse as a Direct Query engine with receipts, evidence, cost and parity (#225, PR 4) (#421)

### Changed
- `repo`: Gate the case matrix, the PR size and data reads outside DuckDB (#265)
- `ci`: Verify locally with the official DuckDB extensions and one check list (#266)
- `server`: Use the address the started server announces, and wait until it answers (#274)
- `repo`: No required check can be skipped silently; scoped checks report "Not applicable" (#269)
- `web`: Bump eslint from 10.13.0 to 10.14.0 in /web (#315)
- `web`: Bump @sveltejs/adapter-static from 4.0.0 to 4.0.1 in /web (#322)
- `web`: Bump @sveltejs/kit from 3.1.0 to 3.1.1 in /web (#321)
- `repo`: Bump hypothesis from 6.170.12 to 6.170.14 (#314)
- `repo`: Bump datamodel-code-generator from 0.86.0 to 0.87.0 (#316)
- `repo`: Bump mypy from 2.4.0 to 2.4.1 (#317)
- `repo`: Bump tzdata from 2026.5 to 2026.6 (#318)
- `repo`: Bump types-pyyaml from 6.0.12.20260906 to 6.0.12.20261030 (#319)
- `semantic`: Generated questions against a pure-Decimal oracle and plain DuckDB (P2-28) (#369)
- `ci`: Add test profiles, impact selection and timing records (#378)
- `store`: Prove the work bounds instead of timing them on a shared runner (#381)
- `server`: Read the run records again only when the lake's catalog changes (#383)
- `repo`: Freeze shared fixtures, run in random order, prove it on Windows (#379)
- `repo`: Cancel a pull request's superseded runs (#388)
- `ci`: Track test-time trends and follow nightly failures (#380)
- `repo`: Run the whole suite on 3.15 and the pr selection on 3.14 in pull requests (#392)
- `repo`: Fail when a raised contract error code has no test (#397)
- `repo`: Fast pull requests — the change's selection on every Python, spread over every part (#401)
- `repo`: Warn on large files, fail on import cycles and façade imports (#398)
- `ci`: Faster store tests, earlier test services and a fresh durations record (#403)
- `semantic`: Add a weekly read-only watch of apache/ossie (#402)
- `repo`: One purpose per file, enforced — first rule for agents, purpose check, size ceiling that only shrinks (#246) (#409)
- `transform`, `repo`: Split incremental and the boundary checks by purpose; one transform SQL parser (#246) (#414)
- `ci`: Fall back to round robin on a corrupt or stale duration record (#261) (#415)
- `store`: Split lake.py and the source worker by purpose, no behavior change (#246, PR 2) (#419)
- `server`: Split commands.py by command, no behavior change (#246, PR 3) (#420)

### Fixed
- `store`: Refuse DuckDB sources with generated columns before any read (#273)
- `semantic`: Write schema-qualified sources as separate identifiers (P2-26c) (#361)
- `semantic`: Refuse a page cursor that is not its canonical Base64 (#387)
- `transform`: Refuse a lookback that is not whole days on a DATE time column (#390)
- `store`, `server`: Record the snapshot a transaction committed, found by its own token (#393)
- `store`: Refuse a Parquet decimal DuckDB does not read as one (#389)
- `store`: Keep the engine's catalog and settings out of SQL, redact escaped secrets (#395)
- `server`: Commit a successful run's records with its data (#406)
- `store`, `server`: Refuse nested UHUGEINT, read policies strictly, isolate source copies (#412)
- `conformance`: Run CASE-008 on every route and fail a runner-side not-run (#423)

### Security
- A DuckDB-file source with generated columns is now refused (`STORE_SOURCE_INVALID`), before anything is read (#268). A `VIRTUAL` generated column's expression ran in the lake's session when the column was read, and could return the lake session's settings. `STORED` generated columns are refused too, as a precaution: no leak through them was shown, and DuckDB 2.0.0 cannot create them.
- `web`: Override minimatch to 11.0.5 for GHSA-g3hv-75wg-m249 (#242, PR 1) (#411)

### Documentation and process
- `repo`: Write the P2 working rules approved by the Owner (#264)
- `adr`: ADR-0029, the semantic engine scope for v1.0.0 is S1–S14 (#275)
- `repo`: Study prior art in proportion to complexity, novelty and risk (#279)
- `adr`: ADR-0030, evolving with Apache Ossie through a version registry (#284)
- `repo`: Record the P2 query-performance baseline (#364) (#368)

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
