# 0027 — Two data routes: managed DuckLake and Direct Query

**Status:** Accepted (2026-10-28). Direct Query on ClickHouse is accepted as direction only, for after v1.0.0. Supersedes ADR-0006 in part ("DuckDB is the only analytical engine" and "Quack is an optional adapter of `store`"). It will supersede ADR-0026 rule 1 in part ("the semantic engine generates only DuckDB SQL") when the ClickHouse adapter is built; until then every engine in v1.0.0 speaks DuckDB SQL. The rest of both records stands.

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
- **Sources:** CSV, Parquet, PostgreSQL, DuckDB file and SQLite (unchanged). ClickHouse also remains a Route A source through extraction models (ADR-0026 rule 3), as an option beside Direct Query.
- **Lake location**, declared in `project.yaml`, within what DuckLake supports:

  | Catalog | Data files | Use |
  |---|---|---|
  | DuckDB file in the project (default) | Folder in the project (default) | Local, one process (today's behavior) |
  | DuckDB file in the project | Object storage | Hybrid: one machine, data remote |
  | PostgreSQL | Object storage | Remote: several processes or machines |
  | PostgreSQL | Local folder | Hybrid: several processes on one machine only |

  - Object storage is reached through DuckDB's official extensions.
  - Credentials are `${env:NAME}` references, turned into temporary DuckDB secrets that are never persisted.
  - When processes run on different machines, the data path must be absolute or an object-storage URL, so every process sees the same files.
  - **Not offered:** a MySQL catalog (DuckLake documents known issues) and a catalog served through Quack (experimental in DuckLake). A SQLite catalog serves several local clients, which Orvex's single-process local mode does not have; it can be added if that changes.

### 3. Route B engines
- **Connections.** A Direct Query connection is declared in `connections.yaml`:
  - it is read-only: `read_only: true` is required, as for every database source;
  - secrets are `${env:NAME}` references;
  - the engine's own read-only and resource limits are verified on connection, as the ClickHouse source already does (`readonly`, `max_execution_time`, `max_result_rows`, `max_memory_usage`).
- **Engines and dialects:**

  | Engine | Dialect generated | Data version read |
  |---|---|---|
  | Existing DuckLake (attached read-only) | DuckDB SQL | A pinned DuckLake snapshot |
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

### 5. Receipts and reproducibility (law 4)
- **Existing DuckLake:** the receipt names the snapshot read and is `exact` while that snapshot is retained by its owner, `explainable` afterwards.
- **Remote DuckDB, and ClickHouse after v1.0.0:**
  - the receipt names the engine, connection and read instant, and is always `explainable`;
  - this applies to certified metrics too (Owner decision);
  - every visual on live data shows "live, read at HH:MM" instead of "data as of".
- **Detail-row pagination on live engines** keeps the deterministic order. Rows may move between pages if the data changes between requests; the receipt of each page carries its read instant.

### 6. Security (law 3)
- The semantic engine stays the single enforcement point. Row and column policies are applied in the plan before the dialect adapter, so every engine receives the same policy placement.
- **Mapping tables.** Mapping tables live in the managed lake (`security.md` §3.3). For a dataset on another engine, the policy's subquery on the mapping table is evaluated first on the managed lake. Its values are bound as parameters of the engine's query: never concatenated, and never by joining engines.
- **Fail closed.** A policy that cannot be placed safely on a dataset's engine makes that dataset unusable (`POLICY_NOT_SUPPORTED_ON_ENGINE`, at model validation), never visible without the policy.
- The security suite runs against every enabled engine.

### 7. Cache, evidence and cost on live engines
- **Cache (ADR-0012):**
  - Live datasets have no run that could invalidate the cache, so their results are cached only when the dataset declares `cache.ttl`, and for at most that time. The key carries the read instant instead of a snapshot.
  - Existing-DuckLake datasets are keyed by snapshot, as today.
- **Evidence** (uniqueness, cardinality, temporal non-overlap, bridge totals):
  - On live engines, evidence is verified by a scheduled verification (period declared per connection, default daily) instead of after transformation runs.
  - The receipt carries the verification instant.
  - Violated evidence blocks dependent queries (`EVIDENCE_VIOLATED`) exactly as on the lake.
- **Cost estimation (law 7):**
  - For a DuckDB-dialect engine, from DuckDB's `EXPLAIN` and, for DuckLake, its statistics.
  - For ClickHouse (after v1.0.0), from `EXPLAIN ESTIMATE` where the table engine supports it.
  - In every case, the budgets are also enforced by the engine's own limits while the query runs.
- **Pre-aggregations (S5)** apply only to datasets in the managed lake in v1.0.0.

### 8. Module boundaries
- `semantic` owns the dialect adapters: plan → SQL.
- `store` owns execution on every engine: the managed lake, attached DuckLakes, Quack and, after v1.0.0, ClickHouse. It keeps one execution boundary that returns Arrow (ADR-0025).
- When the ClickHouse adapter is built, code shared between `transform`'s ClickHouse extraction and `store`'s ClickHouse execution (connection verification, limits, type mapping) moves behind a contract or into `store`. Modules still never import each other's internals.

### 9. Order
- **P1:** the configurable lake location (§2) completes Route A. #98 (running extraction models into the lake) is completed.
- **P2:** Route B is built after the DuckDB adapter passes the corpus. The order is existing DuckLake, then remote DuckDB through Quack; both speak DuckDB SQL. #118 is superseded by this record; #107 becomes the Quack engine for Direct Query in P2.
- **After v1.0.0 (Owner decision):** the ClickHouse dialect adapter and Direct Query on ClickHouse. Until then ClickHouse data reaches the semantic engine by extraction into the managed lake, with at least the 5-minute freshness of a scheduled run. This record fixes its design, so adding it needs no redesign.

## Alternatives considered
- **Copy everything into DuckLake (P1 design):** one engine and exact receipts everywhere, but it duplicates data teams already serve and delays every question until a copy has run.
- **Direct Query only, no managed lake:** no reproducible receipts, no write–audit–publish, no transformation; it drops what makes Route A trustworthy.
- **Transpile DuckDB SQL into each dialect (SQLGlot):** rejected again for the reasons of ADR-0026: transpilation can change numbers silently. Generating from the plan keeps one meaning per construct.
- **Join across engines inside Orvex:** convenient, but it materializes data in Python (ADR-0025), can exhaust memory, and makes the result depend on two engines' semantics at once.
- **Certified metrics only with exact receipts:** stricter, but the Owner chose explainable receipts for all live metrics, with the live read instant always visible.

## Consequences
- The pipeline in `semantic-engine.md` gains one adapter per dialect after the planner. The planner itself stays engine-independent.
- New stable error codes, from P2: `CROSS_ENGINE_QUERY`, `POLICY_NOT_SUPPORTED_ON_ENGINE`.
- The corpus and the security suite become per-engine checks. An engine without a green run is not offered.
- The ClickHouse work of P1 (read-only verification, limits, exact types, server-side parameters) will be reused by the Direct Query adapter after v1.0.0.
- In v1.0.0 the semantic engine still generates only DuckDB SQL. The dialect adapter boundary exists from P2, so ClickHouse is a new adapter, not a change to the planner.
- "Freshness" for live datasets is the read instant. Freshness rules based on runs apply to Route A only.
