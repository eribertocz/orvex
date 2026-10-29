# 0027 — Two data routes: managed DuckLake and Direct Query

**Status:** Accepted (2026-10-28). Direct Query on ClickHouse and on a remote DuckDB through Quack is accepted as direction only, for after v1.0.0 (§10).

This record changes earlier ADRs as follows:
- **ADR-0005** is amended in part: the catalog listed for each runtime mode becomes that mode's default, and the other locations of §2 can be chosen. Opt-in, so local mode still needs no external service by default.
- **ADR-0006** is superseded in part:
  - "DuckDB is the only analytical engine" and "Quack is an optional adapter of `store`";
  - its catalog per mode becomes a default, as in ADR-0005.
- **ADR-0012** is amended in part: the cache key and invalidation for datasets outside the managed lake (§7).
- **ADR-0025** has its rule 6 applied to row-policy values (§6). It will be amended in part (rule 1) when ClickHouse execution, which runs outside DuckDB, is built after v1.0.0.
- **ADR-0026** will be superseded in part when the ClickHouse adapter is built after v1.0.0:
  - rule 1: the engine will also generate ClickHouse SQL;
  - rule 3: ClickHouse will no longer be reachable only through extraction models;
  - the second sentence of rule 4: a future source with its own dialect may also be queried directly through a dialect adapter.

  Until then, every engine in v1.0.0 speaks DuckDB SQL.

The rest of these records stands.

## Context
Through P1, every number the semantic engine returns comes from DuckLake. Data that already lives in an analytical engine therefore had to be copied first. ClickHouse could only be copied, through extraction models (ADR-0026 rule 3), and a remote DuckDB had no defined role at all (#118). Teams with an existing ClickHouse, a DuckDB served through Quack or a DuckLake of their own would have to duplicate it before asking a single question.

The design already isolates the engine: "engine-independent up to the adapter: replacing DuckDB requires a new adapter only" (`semantic-engine.md` §4). The receipt already distinguishes a DuckLake snapshot from "a live source and read instant", and an `exact` reproducibility from an `explainable` one (law 4, `traceability.md` §1).

The Owner decided the product questions in #119.

## Decision

### 1. Two routes, one semantic engine
| Route | Path | Engines in v1.0.0 | After v1.0.0 | Copy |
|---|---|---|---|---|
| **A — Managed lake** | Raw sources → SQL models (light ELT, `transformation.md`) → DuckLake managed by Orvex | DuckDB on the managed DuckLake | — | Always: that is the route |
| **B — Direct Query** | Existing analytical engine → queried in place | An existing DuckLake, attached read-only | Remote DuckDB through Quack; ClickHouse | None required |

Both routes end in the same semantic engine: the same Apache Ossie model, the same correctness engine, the same security, receipts and error catalog. Only the final adapter and the execution target differ.

### 2. Route A sources and lake location
- **Sources:** CSV, Parquet, PostgreSQL, DuckDB file and SQLite (unchanged), plus ClickHouse through extraction models (ADR-0026 rule 3).
  - In v1.0.0, extraction is the only route for ClickHouse.
  - After v1.0.0, Direct Query becomes ClickHouse's default route, and extraction stays an option (Owner decisions 2 and 6).
- **Lake location**, declared in `project.yaml`. Orvex offers every data-file location DuckLake supports, with the catalogs DuckLake considers stable (Owner decision 7):

  | Catalog | Where the catalog lives | Processes it serves | Data files |
  |---|---|---|---|
  | DuckDB file (default in local mode) | Local disk, in the project by default | One process | Any location below |
  | SQLite | Local disk | Several processes on one machine (local or server mode) | Any location below |
  | PostgreSQL (default in server mode) | The same machine, a server on the network, or a managed cloud service (for example AWS RDS, Google Cloud SQL, Azure Database for PostgreSQL) | Several processes on one or several machines | Any location below |

  | Data files | Machines that can share the lake |
  |---|---|
  | Folder in the project (default) or another local folder | One |
  | Network file system (NFS, SMB), mounted at the same path on every machine | Several |
  | Object storage: AWS S3 and S3-compatible stores (for example Cloudflare R2, Hetzner Object Storage), Google Cloud Storage, Azure Blob Storage | Several |

  Rules for these combinations:
  - **Machines.** Several machines need PostgreSQL and shared data files: a network file system or object storage. Validation rejects any combination that cannot serve the declared mode.
  - **Catalog files on a local disk.** DuckDB and SQLite catalogs are files on a local disk, never on a network file system or in object storage. This is an Orvex rule, not a DuckLake one. Both databases rely on file locking for consistency, and locking is unreliable on network file systems and absent in object storage. Only PostgreSQL can host a remote or cloud catalog.
  - **TLS for remote catalogs.** A PostgreSQL catalog on any host other than the local machine is reached with TLS in `sslmode=verify-full`. The certificate authority comes from the system store or a file named by `${env:NAME}`. `require` and `verify-ca` are refused, because they do not authenticate the server, and this connection carries the catalog password and all snapshot metadata.
  - **Extensions and credentials.** Object storage is reached through DuckDB's official extensions. Credentials are `${env:NAME}` references, turned into temporary DuckDB secrets and never persisted.
  - **Encryption.** DuckLake's data-file encryption (`encrypted: true`) is offered and recommended for storage outside the organization, with these limits stated to the user:
    - the keys are stored in the catalog, so encryption protects the files only from someone without catalog access;
    - the mode is fixed when the lake is created, and a different mode for an existing lake is rejected at validation;
    - deletion-vector files (experimental `WRITE_DELETION_VECTORS`) are written unencrypted, so Orvex keeps that option off.
  - **Not offered:**
    - a MySQL catalog, which DuckLake does not recommend because of known issues;
    - a catalog served through Quack, which is experimental in DuckLake.

    Either is added only when DuckLake declares it stable (Owner decision 7).

### 3. Route B in v1.0.0: an existing DuckLake
- **Connection.** An existing DuckLake is a lake owned and written by another system and declared in `connections.yaml`:
  - `read_only: true` is required, as for every database source;
  - secrets are `${env:NAME}` references.
- **Accepted catalogs:**
  - **PostgreSQL**, on any host. A remote host uses the TLS rule of §2.
  - **SQLite**, on a local disk.
  - **A DuckDB-file catalog is refused.** DuckLake allows only one process to attach it unless every process attaches it read-only, so Orvex's read would conflict with the owner's writer.
  - **MySQL and Quack catalogs are refused,** as in §2.
- **Read-only, proven.**
  - The lake is attached with DuckDB's `READ_ONLY` and without automatic catalog migration.
  - A catalog version the bundled DuckLake cannot read without migrating is refused with `ENGINE_VERSION_UNTESTED`, and so is any version the corpus has not run on.
- **Isolation (law 3).** DuckLake stores views as DuckDB SQL that the reader binds, so a view in a lake Orvex does not own could reach anything attached beside it. Two rules prevent this:
  - **Separate instance.** Each existing DuckLake is attached in a DuckDB instance of its own, holding no other catalog and only that lake's secrets.
  - **Tables only.** Semantic datasets may reference only its tables; a dataset on a view is refused with `STORE_SOURCE_INVALID`, the rule already applied to DuckDB and SQLite sources that define views (`contracts/store/v1`).
- **Data version.** Queries read the latest **verified** snapshot (§7).
- **Dialects without transpilation.**
  - The semantic engine keeps one engine-independent plan and has one adapter per dialect, each generating SQL directly from that plan with bound parameters only.
  - In v1.0.0 the only dialect is DuckDB SQL. SQL is never translated from one dialect to another (ADR-0026 rule 4, first sentence).
  - Builders write no SQL for Route B; the semantic model names the connection and table.
- **One engine per dataset.**
  - Each semantic dataset belongs to exactly one engine, given by its connection.
  - A relationship between datasets on different engines is rejected when the model is validated, and a query that would need two engines is rejected; both with `CROSS_ENGINE_QUERY`.
  - The message suggests copying the table into the managed lake (Route A).
  - Orvex never joins results from two engines itself (ADR-0025).

### 4. Exact numbers across engines (laws 4 and 11)
- An engine is enabled only when **the whole semantic corpus** (`conformance/semantic-corpus/`) passes on it, on each tested version, with results identical to the managed-lake reference: same values, same types, same `NULL`s, same errors.
- The security suite exists from P3 (P3-01, P3-13). An engine enabled in P2 is for development only, and is not offered in any release until the security suite also passes on it.
- Differences between dialects are handled in the adapter and pinned by those cases, never discovered in production. Examples, for later engines, are integer division, rounding, `NULL` in aggregates, time zones, `Decimal` precision and `UInt64`.
- Type mapping is explicit per engine; an unmappable column fails with `UNSUPPORTED_SOURCE_TYPE` (law 11).

### 5. Receipts, freshness and reproducibility (law 4)
- **Existing DuckLake:**
  - the receipt names the snapshot read and, when row policies apply, the managed-lake snapshot of each mapping table read (§6);
  - it is `exact` while all of those snapshots are retained (the external one by the lake's owner), and `explainable` afterwards;
  - freshness is the commit time of the snapshot read.
- **Live engines (after v1.0.0):**
  - the receipt names the engine, connection and read instant, and is always `explainable`;
  - this applies to certified metrics too (Owner decision 3);
  - freshness is the read instant, and visuals on live data show "live, read at HH:MM" instead of "data as of";
  - detail-row pagination keeps the deterministic order, but rows may move between pages when the data changes between requests; each page's receipt carries its read instant.

### 6. Security (law 3)
- The semantic engine stays the single enforcement point. Row and column policies are applied in the plan before the dialect adapter, so every engine receives the same policy placement.
- **Mapping tables.** Mapping tables live in the managed lake (`security.md` §3.3). For a dataset on another engine:
  1. the policy's subquery on the mapping table is evaluated first on the managed lake, at a pinned snapshot;
  2. its values are bound as parameters of the engine's query: never concatenated, and never by joining engines;
  3. the binding keeps the subquery's exact meaning, including its types and its `NULL`s. For example, `x NOT IN (…)` over a set that contains `NULL` still returns no rows;
  4. the receipt records the mapping table's snapshot.

  These values are the only rows Orvex holds between two engines. They are small, bounded results under ADR-0025 rule 6. A per-project cap on their number is stated in the security contract.
- **Fail closed.**
  - An empty value set produces a predicate that is always false, so the caller gets zero rows, as in `security.md` §3.3. The predicate is never dropped.
  - A value set above the cap rejects the query, and the error does not reveal how many values the mapping has.
  - A policy that cannot be placed safely on a dataset's engine makes that dataset unusable, never visible without the policy. The error is `POLICY_NOT_SUPPORTED_ON_ENGINE`, raised at model validation, or at query time for the cap.
- **Security suite cases on every enabled engine:**
  - an empty mapping;
  - a mapping above the cap;
  - a `NULL` in a deny-list mapping;
  - a view in an existing lake;
  - a mapping revoked while a result on that engine is cached.

### 7. Cache, evidence and cost outside the managed lake
- **Cache (amends ADR-0012; built with the cache in P3-10):**
  - **The key.** For every dataset outside the managed lake, the key is:
    - the query fingerprint;
    - the model fingerprint;
    - the permission fingerprint;
    - the engine data version: the verified snapshot for an existing DuckLake, none for a live engine;
    - the last-change snapshot of every managed-lake mapping table its policies read.
  - **Checked on every lookup.** The mapping-table snapshots are compared with the current ones on every lookup, without relying on run events. A mapping changed by a run or directly by the Builder is therefore never served from cache.
  - **Live datasets (after v1.0.0)** are cached only when they declare `cache.ttl`. An entry records its read instant and is served while its age is below the `ttl`, and the receipt shows the original read instant.
- **Evidence** (uniqueness, cardinality, temporal non-overlap, bridge totals):
  - **Existing DuckLake.** Orvex checks the attached catalog for a new snapshot on the connection's schedule. It verifies evidence on that snapshot and only then serves it, so no number silently depends on violated evidence, exactly as on the managed lake.
  - **Live engines (after v1.0.0).** Evidence is verified on a schedule declared per connection (default daily). The receipt carries the verification instant: data changed since then is not yet checked, and the receipt says so.
  - **Violated evidence** blocks dependent queries (`EVIDENCE_VIOLATED`), exactly as on the lake.
- **Cost estimation (law 7)** for an existing DuckLake comes from DuckDB's `EXPLAIN` and DuckLake's statistics. Budgets are enforced while running by the separate instance's own DuckDB limits and cancellation, as for the managed lake.
- **Pre-aggregations (S5)** apply only to datasets in the managed lake in v1.0.0.

### 8. Module boundaries
- `semantic` owns the dialect adapters: plan → SQL.
- `store` owns execution on every engine, including the separate DuckDB instances of existing DuckLakes. It keeps one execution boundary that returns Arrow (ADR-0025).
- When the ClickHouse adapter is built, code shared between `transform`'s ClickHouse extraction and `store`'s ClickHouse execution (connection verification, limits, type mapping) moves behind a contract or into `store`. Modules still never import each other's internals.

### 9. Order
- **P1:**
  - the configurable lake location (§2, P1-22) completes Route A;
  - #98 (running extraction models into the lake) stays in P1, as planned.
- **P2:**
  - the Direct Query foundation (P2-29): engine per dataset, `CROSS_ENGINE_QUERY`, receipts, evidence and policy values;
  - then an existing DuckLake (P2-30), after the managed-lake adapter passes the corpus.
- **P3:** the cache rules of §7 are built with the cache (P3-10), and the security suite runs on every enabled engine (P3-13).
- **#118** is superseded by this record. **#107** (Quack) moves after v1.0.0 (Owner decision 8).

### 10. After v1.0.0 (direction only)
This design is fixed now, so adding either engine later needs no redesign.

- **Remote DuckDB through Quack** (Owner decision 8):
  - Read-only access and budgets must be enforced on the server: its databases attached `READ_ONLY`, an authorization function that accepts only the SELECT statements Orvex generates, and the server's own `memory_limit` and stream-buffer limits. The client contributes cancellation (`quack_cancel`) for the time budget.
  - Transport: HTTPS with a CA-verified certificate or a pinned certificate fingerprint. Unencrypted connections to a host other than the local machine are refused.
  - Remote DuckDB versions are checked against the tested set (`ENGINE_VERSION_UNTESTED`).
  - If any of this cannot be shown, Quack is not offered.
- **ClickHouse** (Owner decision 6):
  - It gets a ClickHouse SQL dialect adapter.
  - Its read-only access and limits use the server-side settings the extraction source already sets and verifies (`readonly = 1` and the execution limits listed in `contracts/model-config/v1`), adapted to query budgets.
  - Cost estimates come from `EXPLAIN ESTIMATE` where the table engine supports it.
  - The corpus and the security suite must give results identical to the reference.
  - Until then, ClickHouse data reaches the semantic engine by extraction into the managed lake, with at least the 5-minute freshness of a scheduled run.

## Alternatives considered
- **Copy everything into DuckLake (P1 design):** one engine and exact receipts everywhere, but it duplicates data teams already serve and delays every question until a copy has run.
- **Direct Query only, no managed lake:** no reproducible receipts, no write–audit–publish, no transformation; it drops what makes Route A trustworthy.
- **Transpile DuckDB SQL into each dialect (SQLGlot):** rejected again for the reasons of ADR-0026: transpilation can change numbers silently. Generating from the plan keeps one meaning per construct.
- **Join across engines inside Orvex:** convenient, but it materializes data in Python (ADR-0025), can exhaust memory, and makes the result depend on two engines' semantics at once.
- **Certified metrics only with exact receipts:** stricter, but the Owner chose explainable receipts for all live metrics, with the live read instant always visible.
- **Every catalog DuckLake can attach, including MySQL and Quack:** maximum coverage. It would put the catalog, where every snapshot is recorded, on implementations DuckLake itself does not recommend or calls experimental (data integrity, P0).
- **Attaching an existing DuckLake in the managed lake's DuckDB instance:** simpler, but its views could reach the managed lake and its mapping tables without policies.

## Consequences
- The pipeline in `semantic-engine.md` gains a dialect-adapter stage after the planner. The planner itself stays engine-independent. In v1.0.0 that stage has one adapter: DuckDB SQL.
- New stable error codes:
  - `CROSS_ENGINE_QUERY`;
  - `POLICY_NOT_SUPPORTED_ON_ENGINE`;
  - `ENGINE_VERSION_UNTESTED`.
- The corpus and the security suite become per-engine checks. An engine without a green run on its tested versions is not offered.
- The ClickHouse work of P1 (read-only verification, limits, exact types, server-side parameters) will be reused by the Direct Query adapter after v1.0.0.
- Freshness rules based on runs apply to Route A only. Route B freshness is a snapshot's commit time, or the read instant on live engines (§5).
