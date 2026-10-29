# 0027 — Two data routes: managed DuckLake and Direct Query

**Status:** Accepted (2026-10-28). Direct Query on ClickHouse is accepted as direction only, for after v1.0.0.

This record:
- **supersedes ADR-0006 in part:** "DuckDB is the only analytical engine" and "Quack is an optional adapter of `store`";
- **amends ADR-0012 in part:** cache key and invalidation for datasets outside the managed lake (§7);
- **applies ADR-0025 rule 6** to row-policy values (§6). It will amend ADR-0025 rule 1 in part when ClickHouse execution, which runs outside DuckDB, is built after v1.0.0;
- **will supersede ADR-0026 rules 1 and 3 in part** when the ClickHouse adapter is built after v1.0.0: the engine will generate ClickHouse SQL, and ClickHouse will no longer be reachable only through extraction models. Until then, every engine in v1.0.0 speaks DuckDB SQL.

The rest of these records stands.

## Context
Through P1, every number the semantic engine returns comes from DuckLake. Data that already lives in an analytical engine therefore had to be copied first. ClickHouse could only be copied, through extraction models (ADR-0026 rule 3), and a remote DuckDB had no defined role at all (#118). Teams with an existing ClickHouse, a DuckDB served through Quack or a DuckLake of their own would have to duplicate it before asking a single question.

The design already isolates the engine: "engine-independent up to the adapter: replacing DuckDB requires a new adapter only" (`semantic-engine.md` §4). The receipt already distinguishes a DuckLake snapshot from "a live source and read instant", and an `exact` reproducibility from an `explainable` one (law 4, `traceability.md` §1).

The Owner decided the product questions in #119.

## Decision

### 1. Two routes, one semantic engine
| Route | Path | Engines | Copy |
|---|---|---|---|
| **A — Managed lake** | Raw sources → SQL models (light ELT, `transformation.md`) → DuckLake managed by Orvex | DuckDB on the managed DuckLake | Always: that is the route |
| **B — Direct Query** | Existing analytical engine → queried in place | An existing DuckLake attached read-only; remote DuckDB through Quack; ClickHouse after v1.0.0 | None required |

Both routes end in the same semantic engine: the same Apache Ossie model, the same correctness engine, the same security, receipts and error catalog. Only the final adapter and the execution target differ.

### 2. Route A sources and lake location
- **Sources:** CSV, Parquet, PostgreSQL, DuckDB file and SQLite (unchanged), plus ClickHouse through extraction models (ADR-0026 rule 3).
  - In v1.0.0 extraction is the only route for ClickHouse.
  - After v1.0.0, Direct Query becomes ClickHouse's default route, and extraction stays an option (Owner decisions 2 and 6).
- **Lake location**, declared in `project.yaml`. Every combination DuckLake supports with a catalog it considers stable is offered (Owner decision 7):

  | Catalog | Where the catalog lives | Processes it serves | Data files |
  |---|---|---|---|
  | DuckDB file (default in local mode) | Local disk, in the project by default | One process (local mode) | Any location below |
  | SQLite | Local disk | Several processes on one machine (local or server mode) | Any location below |
  | PostgreSQL (default in server mode) | The same machine, a server on the network, or a managed cloud service (for example AWS RDS, Google Cloud SQL, Azure Database for PostgreSQL) | Several processes on one or several machines | Any location below |

  | Data files | Machines that can share the lake |
  |---|---|
  | Folder in the project (default) or another local folder | One |
  | Network file system (NFS, SMB), mounted at the same path on every machine | Several |
  | Object storage: AWS S3 and S3-compatible stores (for example Cloudflare R2, MinIO), Google Cloud Storage, Azure Blob Storage | Several |

  Rules for these combinations:
  - **Machines.** Several machines need PostgreSQL and shared data files: a network file system or object storage. Validation rejects any combination that cannot serve the declared mode.
  - **Catalog files.** DuckDB and SQLite catalogs are files on a local disk, never on a network file system or in object storage. Only PostgreSQL can host a remote or cloud catalog.
  - **Remote catalog connections** (any PostgreSQL host other than the local machine) require TLS (`sslmode` `require` or stronger).
  - **Extensions.** Object storage is reached through DuckDB's official extensions.
  - **Credentials.** They are `${env:NAME}` references, turned into temporary DuckDB secrets and never persisted.
  - **Encryption.** DuckLake's data-file encryption (`encrypted: true`) is offered and recommended for storage outside the organization.
  - **Not offered:**
    - a MySQL catalog, which DuckLake does not recommend because of known issues;
    - a catalog served through Quack, which is experimental in DuckLake.

    Either is added only when DuckLake declares it stable (Owner decision 7).

### 3. Route B engines
- **Connections.** A Direct Query connection is declared in `connections.yaml`:
  - it is read-only: `read_only: true` is required, as for every database source;
  - secrets are `${env:NAME}` references.
- **Read-only access is proven, never assumed.** An engine is offered only when Orvex can prove its connection cannot write and can enforce the query budgets. Otherwise the connection is refused (fail closed).
  - **Existing DuckLake:** attached with DuckDB's `READ_ONLY` and without automatic catalog migration. A catalog version the bundled DuckLake cannot read without migrating is refused.
  - **Remote DuckDB through Quack:** a client cannot check the server's read-only mode or set per-query limits. P2-31 must therefore show how both are enforced: Orvex sends only SELECT statements it generated, uses client-side cancellation for the time budget, and requires a server configuration that grants read-only access. If it cannot show this, Quack is not offered.
  - **ClickHouse (after v1.0.0):** the server-side settings the extraction source already sets and verifies (`readonly = 1` and the execution limits listed in `contracts/model-config/v1`), adapted to query budgets.
- **Engine versions (law 2).** A green corpus in CI proves results only for the versions it ran on. The remote DuckDB version, and the catalog version of an existing DuckLake, are checked on connection against the tested set; anything else is refused with `ENGINE_VERSION_UNTESTED`.
- **Engines and dialects:**

  | Engine | Dialect generated | Data version read |
  |---|---|---|
  | Existing DuckLake (attached read-only) | DuckDB SQL | The latest verified snapshot (§7) |
  | Remote DuckDB through Quack | DuckDB SQL | Live |
  | ClickHouse (after v1.0.0) | ClickHouse SQL | Live |

- **Dialects without transpilation.**
  - The semantic engine keeps one engine-independent plan and has one adapter per dialect, each generating SQL directly from that plan with bound parameters only.
  - SQL is never translated from one dialect to another (ADR-0026 rule 4 stands).
  - Builders write no SQL for Route B; the semantic model names the connection and table.
- **One engine per dataset.**
  - Each semantic dataset belongs to exactly one engine, given by its connection.
  - A relationship between datasets on different engines is rejected when the model is validated, and a query that would need two engines is rejected; both with `CROSS_ENGINE_QUERY`.
  - The message suggests copying the table into the managed lake (Route A).
  - Orvex never joins results from two engines itself (ADR-0025).

### 4. Correctness across engines (law 2)
- An engine adapter is enabled only when **the whole semantic corpus** (`conformance/semantic-corpus/`) **and the security suite** pass on that engine with results identical to the DuckDB reference: same values, same types, same `NULL`s, same errors.
- Differences between dialects are handled in the adapter and pinned by those cases, never discovered in production. Examples are integer division, rounding, `NULL` in aggregates, time zones, `Decimal` precision and `UInt64`.
- Type mapping is explicit per engine; an unmappable column fails with `UNSUPPORTED_SOURCE_TYPE` (law 11).

### 5. Receipts, freshness and reproducibility (law 4)
- **Existing DuckLake:**
  - the receipt names the snapshot read;
  - it is `exact` while that snapshot is retained by the lake's owner, and `explainable` afterwards;
  - freshness is the commit time of that snapshot.
- **Remote DuckDB, and ClickHouse after v1.0.0:**
  - the receipt names the engine, connection and read instant, and is always `explainable`;
  - this applies to certified metrics too (Owner decision 3);
  - freshness is the read instant, and every visual on live data shows "live, read at HH:MM" instead of "data as of".
- **Detail-row pagination on live engines** keeps the deterministic order. Rows may move between pages if the data changes between requests; the receipt of each page carries its read instant.

### 6. Security (law 3)
- The semantic engine stays the single enforcement point. Row and column policies are applied in the plan before the dialect adapter, so every engine receives the same policy placement.
- **Mapping tables.** Mapping tables live in the managed lake (`security.md` §3.3). For a dataset on another engine:
  1. the policy's subquery on the mapping table is evaluated first on the managed lake, at a pinned snapshot;
  2. its values are bound as parameters of the engine's query: never concatenated, and never by joining engines;
  3. the receipt records the mapping table's snapshot.

  These values are the only rows Orvex holds between two engines. They are small, bounded results under ADR-0025 rule 6. A per-project cap on their number is stated in the security contract.
- **Fail closed.**
  - An empty value set produces a predicate that is always false, so the caller gets zero rows, as in `security.md` §3.3. The predicate is never dropped.
  - A value set above the cap rejects the query.
  - A policy that cannot be placed safely on a dataset's engine makes that dataset unusable (`POLICY_NOT_SUPPORTED_ON_ENGINE`, at model validation, or at query time for the cap). The dataset is never visible without the policy.
- The security suite runs against every enabled engine, including cases for an empty mapping, a mapping above the cap, and a mapping revoked while a live result is cached.

### 7. Cache, evidence and cost outside the managed lake
- **Cache (amends ADR-0012):**
  - **Live datasets.** Their results are cached only when the dataset declares `cache.ttl`.
    - A cached entry records its read instant and is served while its age is below the `ttl`; the receipt shows the original read instant.
    - The entry also records the managed-lake snapshot of every mapping table its policies read. A run on those tables invalidates it exactly as today, so a revoked mapping is never served from cache.
  - **Existing-DuckLake datasets** are keyed by snapshot, as today. They are invalidated when the verified snapshot advances.
- **Evidence** (uniqueness, cardinality, temporal non-overlap, bridge totals):
  - **Existing DuckLake.** Orvex checks the attached catalog for a new snapshot on the connection's schedule. It verifies evidence on that snapshot, and only then serves it. Queries are pinned to the latest verified snapshot, so the managed-lake guarantee holds: no number silently depends on violated evidence.
  - **Remote DuckDB.** Evidence is verified on a schedule declared per connection (default daily). The receipt carries the verification instant. Data changed since then is not yet checked, and the receipt says so.
  - **Violated evidence** blocks dependent queries (`EVIDENCE_VIOLATED`), exactly as on the lake.
- **Cost estimation (law 7):**
  - For a DuckDB-dialect engine, from DuckDB's `EXPLAIN` and, for DuckLake, its statistics.
  - For ClickHouse (after v1.0.0), from `EXPLAIN ESTIMATE` where the table engine supports it.
  - The time budget is also enforced while the query runs: by the engine's own limits where it has them, and otherwise by cancellation (§3).
- **Pre-aggregations (S5)** apply only to datasets in the managed lake in v1.0.0.

### 8. Module boundaries
- `semantic` owns the dialect adapters: plan → SQL.
- `store` owns execution on every engine: the managed lake, attached DuckLakes, Quack and, after v1.0.0, ClickHouse. It keeps one execution boundary that returns Arrow (ADR-0025).
- When the ClickHouse adapter is built, code shared between `transform`'s ClickHouse extraction and `store`'s ClickHouse execution (connection verification, limits, type mapping) moves behind a contract or into `store`. Modules still never import each other's internals.

### 9. Order
- **P1:**
  - the configurable lake location (§2, P1-22) completes Route A;
  - #98 (running extraction models into the lake) stays in P1, as planned.
- **P2:** Route B is built after the DuckDB adapter passes the corpus. The order is existing DuckLake, then remote DuckDB through Quack; both speak DuckDB SQL. #118 is superseded by this record; #107 becomes the Quack engine for Direct Query in P2.
- **After v1.0.0 (Owner decision 6):** the ClickHouse dialect adapter and Direct Query on ClickHouse.
  - Until then, ClickHouse data reaches the semantic engine by extraction into the managed lake, with at least the 5-minute freshness of a scheduled run.
  - This record fixes the design, so adding it later needs no redesign.

## Alternatives considered
- **Copy everything into DuckLake (P1 design):** one engine and exact receipts everywhere, but it duplicates data teams already serve and delays every question until a copy has run.
- **Direct Query only, no managed lake:** no reproducible receipts, no write–audit–publish, no transformation; it drops what makes Route A trustworthy.
- **Transpile DuckDB SQL into each dialect (SQLGlot):** rejected again for the reasons of ADR-0026: transpilation can change numbers silently. Generating from the plan keeps one meaning per construct.
- **Join across engines inside Orvex:** convenient, but it materializes data in Python (ADR-0025), can exhaust memory, and makes the result depend on two engines' semantics at once.
- **Certified metrics only with exact receipts:** stricter, but the Owner chose explainable receipts for all live metrics, with the live read instant always visible.
- **Every catalog DuckLake can attach, including MySQL and Quack:** maximum coverage, but it would put the catalog, where every snapshot is recorded, on implementations DuckLake itself does not recommend or calls experimental (data integrity, P0).

## Consequences
- The pipeline in `semantic-engine.md` gains one adapter per dialect after the planner. The planner itself stays engine-independent.
- New stable error codes, from P2:
  - `CROSS_ENGINE_QUERY`;
  - `POLICY_NOT_SUPPORTED_ON_ENGINE`;
  - `ENGINE_VERSION_UNTESTED`.
- The corpus and the security suite become per-engine checks. An engine without a green run on its tested versions is not offered.
- The ClickHouse work of P1 (read-only verification, limits, exact types, server-side parameters) will be reused by the Direct Query adapter after v1.0.0.
- In v1.0.0 the semantic engine still generates only DuckDB SQL. The dialect adapter boundary exists from P2, so ClickHouse is a new adapter, not a change to the planner.
- Freshness rules based on runs apply to Route A only. Route B freshness is a snapshot's commit time or the read instant (§5).
