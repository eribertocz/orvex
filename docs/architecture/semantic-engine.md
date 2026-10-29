# Semantic engine (`semantic` module) — the core of Orvex

## 1. Mission

> A user asks for metrics and dimensions. The engine returns the **correct** result — or a **stable semantic error** — with a receipt that explains the definition, grain, filters, relationship choices, policies, plan and data version.

The engine is the single enforcement point for data access (law 3) and the single source of truth for business meaning (law 1). Every consumer — dashboards, matrix, explorer, exports, MCP, external API — uses it. No other path executes analytical SQL for consumers.

**Quality bar:** full parity with SEF (the semantic engine of bi-platform) **plus** capabilities S1–S6 and Orvex-specific advantages. Nothing in this document is optional for v1.0.0.

## 2. References studied

| Reference | Adopted | Improved or rejected |
|---|---|---|
| **SEF (bi-platform)** | Staged compilation with typed intermediate representations; correctness separated from planning; facts, obligations and rejections; grain algebra; additivity classes; fan-out reasoning; canonical fingerprints; the normative corpus | Evidence starts "declared, unverified" in SEF; Orvex **verifies evidence against the data** after each load. SEF lacks S1–S6; Orvex includes them |
| **Power BI (DAX)** | Filter-context modification (`CALCULATE`, `ALLSELECTED`), calculation groups | No implicit context transitions; every scope is explicit and typed |
| **Tableau** | Level-of-detail expressions (`FIXED`, `INCLUDE`, `EXCLUDE`); multiple facts related through shared dimensions | Statistical "explanations" are presented as hypotheses, never as causes |
| **Looker** | Fan-out-safe aggregation; declared drill paths; aggregate awareness; freshness coordination (datagroups) | Exact per-fact aggregation instead of hashed symmetric-aggregate tricks |
| **Qlik Sense** | Alternate states (compare two selections); possible / excluded values while filtering | Not an in-memory associative engine; "not authorized" is never revealed as "excluded" |
| **Cube** | Multi-stage measures; pre-aggregation matching; adversarial tests | No Cube dependency; own typed contracts |
| **dbt MetricFlow** | Metrics defined once, SQL generated per request | Ossie as the native format, not a tool-specific one |

## 3. Input: Apache Ossie plus `ORVEX` extensions

The semantic model is a valid Apache Ossie document (pinned specification commit; see ADR-0009). Ossie defines datasets, fields, dimensions, metrics, relationships and `ai_context`. What Ossie does not define is carried in `custom_extensions` entries with `vendor_name: ORVEX`, whose `data` payload is validated by `contracts/ossie-extensions/v1` (JSON Schema).

| Element | `ORVEX` extension fields |
|---|---|
| Dataset | `grain` (key columns that identify one row), `unique_keys`, `time_roles` (event time, validity interval, snapshot time), `evidence` (see §6), `cache` (`enabled`, `ttl`), `certification` |
| Field | `semantic_type` (identifier, category, quantity, amount, ratio, instant, date…, and geographic: `geo_region` with a `geo_source` naming a project GeoJSON file under `assets/geo/` and its key property, `latitude`, `longitude`), `unit`, `currency`, `nullability` contract, `calendar` for date fields, `pii` classification |
| Metric | `additivity` (`additive`, `semi_additive`, `non_additive`, `recomputable`, `ordered`), `non_additive_axes`, `period_end` rule for semi-additive metrics, `components` for recomputable ratios, `zero_denominator` policy, `rounding` policy, `format`, `owner`, `status` (`draft`, `certified`, `deprecated`), `deprecation` (date, replacement) |
| Relationship | `cardinality` (`many_to_one`, `one_to_one`, `one_to_many`, `many_to_many`), `role` name, `evidence`, `bridge` (weights, weight total, tolerance, normalization scope) for many-to-many allocation, `temporal` (as-of matching with closed–open intervals) |
| Model | `calendars` (Gregorian, fiscal, business calendar tables), `default_time_zone`, `drill_paths`, `calculation_groups`, `preaggregations` (name, base dataset, grain, metrics, refresh strategy; S5) |

Rules:

- Reading an Ossie model **without** `ORVEX` extensions works: missing information takes the safest default, and any query that needs the missing information is **rejected** with a stable error explaining which declaration is missing. The engine never assumes additivity or cardinality.
- Exporting to plain Ossie produces a valid document; the export report lists every `ORVEX` semantic that the target cannot represent (law 12).
- Previous vendor names in `identity.toml` are accepted as aliases.

## 4. Compilation pipeline

```
Ossie YAML ─► Ossie adapter ─► CompiledModel
                                    │
SemanticQuery (JSON) ───────────────┤
SecurityContext (JSON) ─────────────┤
                                    ▼
                     Correctness engine ─► CorrectnessReport (facts, obligations)  ─┐
                                    │                                               │ or SemanticError
                                    ▼                                               │ (stable code)
                              Planner ─► SemanticPlan (logical DAG)                  │
                                    │      └─ every obligation discharged ◄─────────┘
                                    ▼
              Dialect adapter ─► PhysicalQuery (SQL + bound parameters + data version)
              (DuckDB in v1.0.0; ClickHouse after)
                                    ▼
                              Execution (store) ─► Arrow result
                                    ▼
                         Result envelope + Receipt
```

| Stage | Type | Responsibility |
|---|---|---|
| Ossie adapter | `CompiledModel` | Parse, validate, resolve references, type every expression, build the relationship graph, compute fingerprints. Knows Ossie; nothing after it does |
| Query binding | `SemanticQuery` (canonical) | Bind names to model members, normalize filters, canonicalize ordering so equivalent queries have equal fingerprints |
| Correctness engine | `CorrectnessReport` | Decide **whether** the question has one correct answer: derive semantic facts, emit obligations the plan must satisfy, or reject. Does not decide how to execute |
| Planner | `SemanticPlan` | Decide **how**: per-fact aggregation, alignment, joins, recomputation, window steps, policy placement. Must discharge every obligation; an undischarged obligation is a planner error, never a silent result |
| Dialect adapter | `PhysicalQuery` | Generate the SQL of the dataset's engine from the plan, with bound parameters only: DuckDB SQL for the managed lake, an existing DuckLake and remote DuckDB; ClickHouse SQL for ClickHouse after v1.0.0 (ADR-0027). Never translates SQL between dialects. The only stage that knows a dialect |
| Execution | Arrow | Run once through `store` on the dataset's engine, within time and memory budgets: on a fixed DuckLake snapshot for the managed lake and an existing DuckLake, live for remote DuckDB (and ClickHouse after v1.0.0). Data stays in the engine and is read once into Arrow (ADR-0025) |

Properties:

- Every intermediate representation is immutable and typed, with a canonical fingerprint: SHA-256 over the JSON Canonicalization Scheme (RFC 8785).
- Deterministic: same model fingerprint + same query fingerprint + same security fingerprint + same snapshot ⇒ same result. On live engines the data version is the read instant, so the result is explainable, not replayable (ADR-0027 §5).
- Engine-independent up to the adapter: adding an engine requires a new adapter only, enabled once the whole corpus and the security suite pass on it with results identical to the DuckDB reference (ADR-0027 §4).
- **Two data routes (ADR-0027):** datasets come from the managed DuckLake (route A) or from an existing engine queried in place (route B: an existing DuckLake, remote DuckDB through Quack; ClickHouse after v1.0.0). Each dataset belongs to one engine; a relationship or a query spanning two engines is rejected with `CROSS_ENGINE_QUERY`.

## 5. Correctness core (parity with SEF)

| # | Capability | Rule enforced |
|---|---|---|
| C1 | Grain algebra | Every value has a grain; transitions between grains must be legal (refinement or declared aggregation) |
| C2 | Additivity and recomposition | Additive metrics sum; semi-additive metrics use the period-end rule on their non-additive axis; non-additive metrics (distinct counts) are recomputed at the requested grain; ratios are recomputed from components, never averaged |
| C3 | Fan-out and cardinality | A join that could multiply a fact's rows is either proven safe (many-to-one with verified evidence) or isolated by aggregating the fact before joining |
| C4 | Relationship-path disambiguation | When more than one path connects two datasets, the query must name a role; otherwise `AMBIGUOUS_RELATIONSHIP_PATH` |
| C5 | Multi-fact planning | Facts at the same or different grains are aggregated independently to the requested grain and aligned on conformed dimensions |
| C6 | Temporal semantics | Time zone applied before calendar bucketing; period-end semantics; as-of joins over closed–open intervals with exactly-one-match guards; illegal temporal re-aggregation rejected |
| C7 | Many-to-many allocation | Bridge weights validated (total, tolerance, normalization scope) before allocation |
| C8 | Semantic equivalence and safe rewrites | A rewrite (including materialization substitution) is applied only with a proof of equivalence; optimizations never change meaning |
| C9 | Obligation discharge | The plan carries evidence that each correctness obligation is satisfied; the receipt references it |

Additional invariants:

- **Units and currencies** are typed: adding USD to BOB is rejected (`INCOMPATIBLE_UNITS`).
- **Exact numerics:** decimals keep precision and scale end to end; division follows the metric's `rounding` and `zero_denominator` policies.
- **Nulls** follow each aggregate's declared null contract; `NULL` never becomes zero.
- **Totals and subtotals** are computed by the engine (grouping sets) with the same correctness rules as the detail rows.

## 6. Evidence: declared and verified (Orvex advantage)

Every relationship cardinality, dataset uniqueness, temporal non-overlap and bridge-weight total is **evidence** with a status:

| Status | Meaning | Engine behavior |
|---|---|---|
| `declared` | Stated in the model, not yet checked | Usable; the receipt marks the result as relying on unverified evidence |
| `verified` | Checked against the data of a specific DuckLake snapshot | Usable; the receipt references the verification run |
| `violated` | The check failed on the current snapshot | **Not usable.** Queries depending on it are rejected with `EVIDENCE_VIOLATED` and the reason (for example "consumer_id is not unique in dim_consumers: 37 duplicates") |

Because `transform` and `semantic` live in the same product, the engine **re-verifies evidence after every successful transformation run** affecting the involved tables. Datasets on live engines (ADR-0027) have no runs: their evidence is re-verified on a schedule declared per connection (default daily), and the receipt carries the verification instant. On the managed lake, a dashboard can never show numbers that silently depend on a relationship the data no longer satisfies; on a live engine, data changed since the last verification is not yet checked, and the receipt says so by showing that instant.

## 7. Security inside the engine

- The engine receives a `SecurityContext` (user, roles, groups, attributes, permission fingerprint) and never reads users or sessions itself.
- **Row-level policies** are mandatory predicates attached to datasets; the planner places them **before aggregation**, on every path that reads the dataset, including subtotals, totals, distinct counts, possible-values lists and pre-aggregations.
- **Column-level security** has three levels per field or metric: `visible`, `masked` (values replaced by a declared mask) and `hidden` (the member does not exist for that caller; referencing it yields `MEMBER_NOT_FOUND`, indistinguishable from a non-existent member).
- **Non-broadening property:** no user filter, LOD expression, context modifier or comparison state can widen the rows a mandatory policy allows. Verified with property-based tests.
- Missing required policy attributes ⇒ the caller sees **no rows** (fail closed), never all rows.
- **Other engines (ADR-0027 §6).** Policies are placed in the plan, before the dialect adapter, so every engine receives the same placement. For a dataset outside the managed lake, a policy's subquery on a mapping table is evaluated first on the managed lake and its values are bound as parameters of the engine's query; a policy that cannot be placed safely makes the dataset unusable (`POLICY_NOT_SUPPORTED_ON_ENGINE`).
- Policies are declared in `security.yaml` (`contracts/security-policies/v1`) and evaluated by the engine's own evaluator; the contract is designed so a future Cedar-based evaluator can implement it (ADR-0011).

## 8. Capabilities beyond SEF (S1–S6)

### S1 — Filter context, level of detail and calculation groups

- **Context modifiers** on metric expressions: `all(<dims>)` (ignore filters on those dimensions), `all_selected(<dims>)` (respect only user-visible selections), `keep(<filters>)`, `remove(<filters>)`.
- **Level of detail:** `fixed(<dims>)`, `include(<dims>)`, `exclude(<dims>)`, each with an explicit grain checked by grain algebra.
- **Calculation groups:** reusable transformations applied over any compatible metric (for example "% of total", "rank", "difference to previous period") without duplicating metrics.
- Security policies always apply inside every modified context (non-broadening).
- Examples that become corpus cases: share of total by region; customer-level average then regional average; percent of grand total with an active filter.

### S2 — Comparing selections and possible values

- **Named analysis contexts** in one query (for example `A: year = 2025`, `B: year = 2026`) evaluated independently and aligned for comparison.
- **Possible-values API:** for a dimension, return values that are selected, possible and excluded given the current filters — computed only over **authorized** rows. "Not authorized" is never exposed as a category.

### S3 — Time intelligence and business calendars

- Calendars: Gregorian, fiscal (configurable year start) and business calendars defined by a calendar table.
- Operators: previous period, same period last year, period-to-date (YTD, QTD, MTD), rolling *N* periods, cumulative.
- **Coverage rules:** incomplete periods are flagged in results; comparing a partial period with a complete one requires an explicit choice (`aligned` or `full`); without it the query is rejected with `COVERAGE_POLICY_REQUIRED`.
- Time zone (for example `America/La_Paz`) is applied before bucketing.

### S4 — Governed metric authoring

- Lifecycle: `draft` → validated → previewed → impact analysis → `certified` → (`deprecated` → removed).
- **Validation:** the same compiler validates a metric from the UI form, from YAML and from an AI proposal.
- **Preview** on a sample with the author's permissions.
- **Impact analysis** lists dashboards, pages, visuals and metrics affected by a change, and shows value differences before publishing.
- Authors can **propose** metrics; only Builders certify. Publishing writes the Ossie file in the project (Git).

### S5 — Equivalent acceleration

- **Pre-aggregations** declared by the Builder or suggested by the engine from query statistics.
- Used only with a **proof of equivalence** (C8): same grain or a legal roll-up, same metric semantics, same policy columns available so row-level security still applies.
- Refreshed by `transform`; the receipt records that a pre-aggregation was used and its snapshot.
- In v1.0.0 pre-aggregations apply only to datasets in the managed lake (ADR-0027 §7).
- Never changes results (verified by the corpus case on materialization equivalence plus added cases).

### S6 — Explanation and comparison

- **Human-readable receipt:** definition in business terms, grain, filters (security filters described without revealing protected values), relationship choices, evidence status, data version and freshness, reproducibility class.
- **Result comparison:** explain why a number changed between two receipts — model change, data change, filter change or policy change.
- **AI explanation** through MCP uses the receipt as grounding; the AI never invents a calculation.

### Query budgets and cost estimation (law 7)

- Before execution the planner estimates the cost of each `SemanticPlan` from DuckLake statistics (row counts, file sizes, column min/max) and DuckDB's `EXPLAIN` cardinalities: rows scanned, groups produced, cells returned. On ClickHouse (after v1.0.0) the estimate comes from `EXPLAIN ESTIMATE` where the table engine supports it (ADR-0027 §7).
- Budgets per query (rows scanned, result rows, matrix cells, time, memory) are configured per project with safe defaults. A plan whose estimate exceeds a budget is rejected **before** running with `QUERY_BUDGET_EXCEEDED` (or `MATRIX_TOO_WIDE`), stating which budget and the estimate, and suggesting a filter or a coarser grain.
- The time and memory budgets are also enforced while running (the engine's own limits and cancellation), because estimates can be wrong.
- The estimate and the actual figures are recorded in the receipt so wrong estimates can be measured and improved.

## 9. Matrix and totals support

The matrix (see `frontend.md`) sends a `MatrixQuery` (rows, columns, measures, totals policy, expanded paths, viewport). The engine:

- plans grouping sets for detail, subtotals and grand totals in one consistent plan;
- recomputes non-additive metrics and ratios at every total level;
- paginates deterministically without changing totals;
- returns typed cells with role (`detail`, `subtotal`, `total`), nullity and exact values;
- applies row-level security before every aggregation level, so expanding a node never reveals unauthorized rows;
- computes **"show value as"** options (% of row, column or grand total, % of parent, difference and % difference to the previous period) in the plan, never in the browser (law 1).

**Detail-row queries.** `semantic-query` also has a detail mode for tables and drill-through: the caller names fields (no aggregation); the grain is the dataset's declared `grain`; column policies and masking apply (security §3.3); a row budget and a deterministic order (the declared unique key as final tie-breaker) are mandatory, and pagination never repeats or skips rows within one snapshot. On live engines rows may move between pages when the data changes between requests; each page's receipt carries its read instant (ADR-0027 §5).

## 10. Caching

- Level 1: result cache in memory (and optionally on disk in server mode).
- Key: query fingerprint + model fingerprint + data snapshot + **permission fingerprint**.
- In server mode each worker keeps its own cache; a run record is broadcast to every worker through PostgreSQL `LISTEN/NOTIFY`, so no worker serves a result older than the latest committed run.
- Invalidation: when `transform` commits a run affecting a dataset, every cached result depending on it is invalidated; additionally the per-dataset `cache.ttl` extension applies; `cache.enabled: false` disables caching for a dataset.
- Datasets on live engines (ADR-0027) are cached only when they declare `cache.ttl`, and for at most that time; their key carries the read instant instead of a snapshot.
- The receipt states whether a result came from cache.

## 11. Interfaces

The engine is usable in three ways, all over the same contracts:

1. **Library:** `orvex.semantic` Python API.
2. **HTTP:** through `server` (`/api/v1/semantic/query`, `/explain`, `/catalog`, `/possible-values`, `/matrix`).
3. **MCP:** through `mcp` (`describe_model`, `query`, `explain`, `list_dashboards`).

## 12. Error codes (initial set)

`MEMBER_NOT_FOUND`, `AMBIGUOUS_RELATIONSHIP_PATH`, `NO_RELATIONSHIP_PATH`, `INCOMPATIBLE_UNITS`, `ILLEGAL_TEMPORAL_REAGGREGATION`, `ILLEGAL_GRAIN_TRANSITION`, `FANOUT_NOT_ISOLATABLE`, `MISSING_ADDITIVITY_DECLARATION`, `MISSING_CARDINALITY_DECLARATION`, `EVIDENCE_VIOLATED`, `BRIDGE_WEIGHTS_INVALID`, `TEMPORAL_MATCH_NOT_UNIQUE`, `DIVISION_POLICY_VIOLATION`, `DEPRECATED_METRIC` (warning), `METRIC_IN_USE` (deleting or renaming a metric that other metrics reference; for dashboard references, `server` asks `dashboards` before the project writer saves and returns the same code), `MEMBER_MASKED`, `COVERAGE_POLICY_REQUIRED`, `OSSIE_INVALID`, `UNSUPPORTED_OSSIE_VERSION`, `MISSING_ORVEX_DECLARATION`, `POLICY_ATTRIBUTE_MISSING` (notice: the result has zero rows for that policy, never an error that reveals data; security §3.3), `QUERY_BUDGET_EXCEEDED`, `MATRIX_TOO_WIDE`, `UNSUPPORTED_OPERATION`, `CROSS_ENGINE_QUERY` (a relationship or query spanning two engines; ADR-0027), `POLICY_NOT_SUPPORTED_ON_ENGINE` (a row policy that cannot be placed safely on a dataset's engine; the dataset is unusable, never visible without the policy).

Each code has a stable identifier, a human message (i18n) and structured details. Codes are part of `contracts/semantic-error`; removing or changing the meaning of a code is a breaking change.

## 13. Conformance

- **Normative corpus:** the 21 cases of the bi-platform "Semantic Problem Corpus v0.1" (same Owner, Apache-2.0) are imported unchanged into `conformance/semantic-corpus/` with attribution. Each case has deterministic CSV input and either an exact expected result or an expected error.
- **New cases** for S1–S6, the matrix, security and evidence verification are added as `CASE-101` onward, following the same format: semantic intent, input, exact expected output or error. Existing cases change only through an ADR proving their business semantics were wrong.
- A case passes only if both the behavior and the exact result or error match.
- Any future implementation of the engine (for example in Rust) must pass the same corpus to replace this one.

## 14. Parity and advantage summary

| Topic | SEF today (code reviewed 2026-10-26) | Orvex v1.0.0 |
|---|---|---|
| Corpus | 17 of 18 valid cases executed against the oracle (distinct count missing); 3 rejection cases in correctness tests | **21 of 21**, plus CASE-101+ |
| Evidence | Declared, unverified | **Verified against data after every load** |
| S1–S6 | Proposed, not implemented | **Implemented** |
| Column-level security | Pending | **Visible, masked, hidden** |
| Receipt | Designed | Implemented and shown on every number |
| Ossie | Adapter | Native format with `ORVEX` extensions and loss-reporting export |
