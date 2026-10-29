# Roadmap (tentative)

**Status:** tentative plan approved for execution. The **order**, **versions**, **exit criteria** and **issue list** are binding; dates are intentionally absent (quality before speed). Every issue is **medium** ([`process/issues.md`](process/issues.md) §2); if an issue turns out larger, the Agent splits it and records the split in the issue. Changes to phases or versions require the Owner.

## 1. Overview

| Phase | Name | Main modules | Issues | Ends with version | Stage |
|---|---|---|---|---|---|
| P0 | Foundation | repo, web, contracts, server, conformance | 11 | `v0.1.0-alpha.1` | Alpha |
| P1 | Storage and transformation | store, server, transform | 22 | `v0.2.0-alpha.1` | Alpha |
| P2 | Semantic engine — parity with SEF | semantic, store, access, server | 30 | `v0.3.0-alpha.1` | Alpha |
| P3 | Access and security | conformance, access, semantic, server | 14 | `v0.4.0-alpha.1` | Alpha |
| P4 | Semantic engine — S1, S3, S6 | conformance, semantic | 12 | `v0.5.0-alpha.1` | Alpha |
| P5 | Semantic engine — S2, S4, S5 | conformance, semantic, server, transform | 12 | `v0.6.0-alpha.1` | Alpha |
| P6 | Web foundation and dashboards | web, dashboards, server | 21 | `v0.7.0-alpha.1` | Alpha |
| P7 | The matrix | conformance, semantic, web, server | 12 | `v0.8.0-alpha.1` | Alpha |
| P8 | Studio | server, web | 10 | `v0.9.0-alpha.1` | Alpha |
| P9 | AI and operations | mcp, access, server, repo | 11 | `v0.10.0-alpha.1` | Alpha — MVP scope complete |
| P10 | Beta: pilot and hardening | all | 6 | `v0.11.0-beta.1` … `beta.K` | Beta |
| RC | Release candidates | all | defects only | `v1.0.0-rc.1` … `rc.K` | Release candidate |
| **GA** | **MVP** | all | — | **`v1.0.0`** | **Stable** |

**Total: 161 medium issues**, plus defects found along the way.

**What version is the finished MVP?** When all MVP scope is implemented (end of P9) the product enters **beta** (`v0.11.0-beta.1`) with pilot users. The MVP is ready when the beta has no open `p0`/`p1` defects and the ten release criteria of [`process/versioning.md`](process/versioning.md) §4 are evidenced and approved by the Owner. That release is **`v1.0.0` (stable)**.

## 2. Rules shared by every phase

- **Milestone:** each phase is a GitHub milestone (P0–P10, RC, GA).
- **Phase start (Agent):** verify every dependency is at its latest stable release (ADR-0022); create all issues of the phase in outline; open one `type:decision` issue with the phase's new product behaviors for the Owner's batch approval; fully specify the first three issues.
- **During the phase:** keep the next three issues fully specified (`process/issues.md` §3); implement in the listed order unless dependencies allow otherwise; issues whose title starts with **"Normative cases"** define expected semantics, touch governed paths and are merged by the Owner — they always come **before** the code they test.
- **Endpoints:** every issue that adds a capability reachable over HTTP also adds its endpoint to the `http-api` contract and its generated client.
- **Phase end (Agent):** open the release issue with the exit checklist and follow [`process/git-workflow.md`](process/git-workflow.md) §6.
- **Owner demos** at the end of P1, P2, P6, P7, P8 and P9, on synthetic data and on real data locally.
- **Independent review** of the phase's documents and code by a Reviewer pass before tagging each alpha.
- **Patch releases** (`v0.N.1-alpha.1`, …) only for defects.

---

## P0 — Foundation → `v0.1.0-alpha.1`

**Goal:** a repository where every later issue can be specified, implemented, verified and released under the rules.
**Required reading:** `AGENTS.md`; `docs/process/`; module packs `repo.md`, `web.md`, `contracts.md`, `server.md`, `conformance.md`.
**Owner tasks before P0-01:** create the empty repository `eribertocz/orvex`; merge the foundation pull request; formal trademark search for the product name (before the first public tag).

| ID | Issue | Module |
|---|---|---|
| P0-01 | Repository governance: branch-protection settings prepared by the Agent and applied by the Owner (including "do not allow bypassing"), labels from `.github/labels.yml`, milestones P0–GA, `governance` required check for governed paths, status-label automation, Dependabot (weekly; security immediately) | repo |
| P0-02 | Python workspace: one `pyproject.toml` per package under the `orvex` namespace, latest stable Python, `pylock.toml`, `ruff`, `mypy --strict`, `pytest`; CI jobs for them | repo |
| P0-03 | Web workspace: SvelteKit (static adapter), Svelte 5, TypeScript strict, Tailwind CSS v4, `svelte-check`, lint, unit tests; CI jobs; dependency license allow-list | web |
| P0-04 | Identity and boundaries: `identity.toml` loader, rename script, product-name check, import-boundary and materialization checker (standard-library `ast`, ADR-0025) with tests that provoke each violation; CI jobs | repo |
| P0-05 | Contracts workspace: `contracts/<name>/vN` layout, JSON Schema and OpenAPI validation, generated types for Python and TypeScript, CI check that generated types are current | contracts |
| P0-06 | `http-api` contract v1 baseline (OpenAPI 3.1: health, version, error envelope) and generated TypeScript client | server |
| P0-07 | Server skeleton: `orvex` CLI, local mode, FastAPI app, composition root, serving the web build, JSON logging with request id, instance directory `~/.<slug>/` (override `<SLUG>_HOME`), `orvex <path>` opens or creates a project folder and registers it with the instance (`project.yaml` is written once its contract exists, P1-05), temporary directory for in-memory projects | server |
| P0-08 | Versions and releases: `versions.toml`, module and contract version declarations, startup compatibility check, tag-triggered release workflow (GitHub release, Docker image skeleton) | repo |
| P0-09 | Synthetic data generator, part 1: consumers and meter readings (deterministic seed; late readings, nulls, duplicates, estimated readings) | repo |
| P0-10 | Synthetic data generator, part 2: billing and collections (voided invoices, negative balances, partial payments), scalable to 100 M rows with measured generation time | repo |
| P0-11 | Semantic corpus import (bi-platform commit `072bb2c7502ae0f46a60a3156724a07e058b3d3d`, unchanged, with `NOTICE`), Apache Ossie specification pinned and its schema vendored, corpus runner with a non-governed adapter (format, vendor-name alias and error-code mapping to Orvex's) reporting per case `not-run` until P2-12 *(touches governed paths: Owner merges)* | conformance |

**Exit criteria:** all P0 required checks green (`engineering.md` §8); `orvex` opens an empty UI in local mode; the corpus runner loads all 21 cases and reports them `not-run`; governance check blocks unlabeled pull requests on governed paths; rename script and product-name check pass; trademark search done.

---

## P1 — Storage and transformation → `v0.2.0-alpha.1`

**Goal:** sources become tested, versioned tables in DuckLake, loaded fully or incrementally on declared freshness.
**Required reading:** module packs `store.md`, `transform.md`.

| ID | Issue | Module |
|---|---|---|
| P1-01 | `store` contract v1 and DuckLake adapter with local DuckDB catalog: transactions, pinned snapshot ids, lazy relations read once into Arrow (ADR-0025) | store |
| P1-02 | DuckLake adapter with PostgreSQL catalog; concurrent readers and writers across processes | store |
| P1-03 | Exact type mapping DuckDB → Arrow → JSON (`DECIMAL`, `BIGINT`, `UBIGINT`, `HUGEINT` as strings; `TIMESTAMPTZ`; `NULL`) | store |
| P1-04 | Query budgets and admission control: memory and thread limits, timeouts, cancellation, worker-thread execution | store |
| P1-05 | `project` contract v1 (`project.yaml`, `connections.yaml`; other modules add their own sections by extending it) and loader with environment-variable secrets | server |
| P1-06 | File sources: Parquet, CSV, DuckDB | transform |
| P1-07 | Database sources: SQLite and PostgreSQL attached read-only (`SOURCE_NOT_READ_ONLY` when `read_only: true` is missing) | transform |
| P1-08 | `model-config` contract v1 and model discovery: naming, validation errors, `.py` models rejected with `UNSUPPORTED_MODEL_KIND` | transform |
| P1-09 | Dependency graph with DuckDB's parser (`DEPENDENCY_CYCLE`), materializations `view` and `table`, CLI `orvex validate` and `orvex run` (single-writer rule, `PROJECT_LOCKED`) | transform |
| P1-10 | `run-record` contract v1, persistence and run history; every run from here on produces one | transform |
| P1-11 | Incremental strategies `append` and `merge` | transform |
| P1-12 | Incremental strategies `delete_insert` and `partition_overwrite`; `on_schema_change` policies | transform |
| P1-13 | Automatic incremental filter with provability analysis (`INCREMENTAL_FILTER_NOT_PROVABLE`) | transform |
| P1-14 | Manual filter with `$since`, watermark from loaded rows, `lookback`, full-refresh values per type | transform |
| P1-15 | Model tests and write–audit–publish inside one DuckLake transaction | transform |
| P1-16 | Reconciliation against sources (`RECONCILIATION_FAILED`) | transform |
| P1-17 | ClickHouse extraction models: `clickhouse-connect`, ClickHouse SQL validated with `EXPLAIN SYNTAX`, server-side `{since:…}`, Arrow streaming, exact type mapping | transform |
| P1-18 | OpenLineage events (start, complete, fail; datasets; column lineage where derivable) | transform |
| P1-19 | Scheduler: freshness-driven planning (minimum 5 minutes, `FRESHNESS_TOO_SHORT`), `full_refresh` cron with time zone, no overlapping runs, retries with backoff | transform |
| P1-20 | Snapshot retention (from `project.yaml`) and compaction as maintenance runs | store |
| P1-21 | Scheduler fit to run unattended (clean stop on SIGTERM, heartbeat shown by `orvex runs`, one scheduler per project); load-performance baseline on the `large` reference dataset (decision #33) on the reference machine; end-to-end test of 5-minute incremental loads from PostgreSQL (decision #133) | server |
| P1-22 | Lake location in `project.yaml` (ADR-0027 §2): catalog (DuckDB file or SQLite on a local disk, or PostgreSQL local, networked or cloud with TLS `verify-full`; object storage over HTTPS only) and data files (project folder, local path, network file system, object storage), optional data-file encryption (mode fixed at creation, deletion vectors off), credentials by `${env:NAME}` as temporary DuckDB secrets; every supported combination tested, invalid ones rejected | store, server |

**Exit criteria:** every strategy and every error code introduced in this phase tested; write–audit–publish proven with failing tests; incremental loads from PostgreSQL every 5 minutes on synthetic data without duplicates or gaps; valid lineage events; Owner demo.

---

## P2 — Semantic engine: parity with SEF → `v0.3.0-alpha.1`

**Goal:** the engine answers correctly or rejects with stable errors for all 21 corpus cases, with receipts and verified evidence.
**Required reading:** module pack `semantic.md` (rows "All" and "P2").

| ID | Issue | Module |
|---|---|---|
| P2-01 | `ossie-extensions` contract v1 (`ORVEX` payload JSON Schema, including geographic semantic types and pre-aggregation declarations) | semantic |
| P2-02 | `semantic-error` contract v1 and the stable error catalog (codes, i18n messages, details) | semantic |
| P2-03 | Ossie adapter: parse and validate the pinned specification, resolve references (`OSSIE_INVALID`, `UNSUPPORTED_OSSIE_VERSION`) | semantic |
| P2-04 | `CompiledModel`: typed expressions, semantic types, units and currencies; canonical fingerprints (own RFC 8785 implementation tested with the RFC's examples) + SHA-256 | semantic |
| P2-05 | `semantic-query` contract v1 (aggregate and detail-row modes); query binding and canonicalization | semantic |
| P2-06 | `semantic-result` contract v1 and result envelope (Arrow schema, exact values as strings, masked columns) | semantic |
| P2-07 | `receipt` contract v1 and receipt builder; every result from P2-12 on carries one, later issues extend its fields | semantic |
| P2-08 | `security-policies` contract v1 and policy model parsing (`security.yaml`), including the explicit registration of deny-list callers with nothing blocked (ADR-0027 §6) | semantic |
| P2-09 | `security-context` contract v1 with a test provider (the real provider arrives in P3) | access |
| P2-10 | `catalog` contract v1 (members visible to a caller) and Ossie export with loss report | semantic |
| P2-11 | Correctness engine framework (facts, obligations, rejections) and grain algebra (C1) | semantic |
| P2-12 | Planner skeleton, DuckDB adapter (plan → SQL with bound parameters only) and execution on a fixed snapshot: CASE-001 end to end; corpus runner switches from `not-run` to real results | semantic |
| P2-13 | Ratio recomputation and null semantics (CASE-006, CASE-014) | semantic |
| P2-14 | Distinct-count non-additivity and semi-additive period-end (CASE-008, CASE-007) | semantic |
| P2-15 | Relationship graph, path resolution, roles and ambiguity rejection (CASE-002, -003, -005, -016) | semantic |
| P2-16 | Fan-out reasoning and isolation; incompatible units rejection (CASE-004, CASE-017) | semantic |
| P2-17 | Multi-fact planning at the same and different grains (CASE-009, CASE-010) | semantic |
| P2-18 | Many-to-many bridge allocation (CASE-011) | semantic |
| P2-19 | Time zone before bucketing, grain conversion, decimal and time-zone edges, illegal temporal re-aggregation (CASE-012, -019, -020) | semantic |
| P2-20 | Temporal as-of joins and cumulative metrics (CASE-013, CASE-021) | semantic |
| P2-21 | Mandatory policy correctness and placement core (CASE-015) | semantic |
| P2-22 | Obligation discharge (C9) across the planner, with failing-plan tests | semantic |
| P2-23 | Totals and subtotals through grouping sets with full correctness | semantic |
| P2-24 | Detail-row queries: row grain, column policies, row budgets, deterministic ordering and pagination | semantic |
| P2-25 | Materialization equivalence proof and substitution (C8; CASE-018) | semantic |
| P2-26 | Evidence verification after runs (`declared` / `verified` / `violated`; `EVIDENCE_VIOLATED`) | semantic |
| P2-27 | HTTP endpoints `query`, `explain`, `catalog`; corpus **21/21** becomes a required check; query performance baseline recorded | server |
| P2-28 | Property-based tests for binder, correctness engine and planner | semantic |
| P2-29 | Direct Query foundation (ADR-0027): engine per dataset, `CROSS_ENGINE_QUERY`, receipts naming every snapshot read, policy values from the managed lake bound as parameters with exact `NULL` semantics and a cap (`POLICY_NOT_SUPPORTED_ON_ENGINE`) | semantic, store |
| P2-30 | Direct Query on an existing DuckLake: PostgreSQL or SQLite catalog attached `READ_ONLY` in a DuckDB instance of its own with a file sandbox, its data location separate from every lake and file source of any project in the instance (checked both ways), its SQLite catalog file a regular file outside them, its PostgreSQL catalog role dedicated and unable to read outside its schema, tables only, catalog version checked (`ENGINE_VERSION_UNTESTED`), served at its latest verified snapshot (evidence verified on each new snapshot); corpus green on it (development only until P3-13) | store, semantic |

**Exit criteria:** corpus **21/21** (including CASE-008, which SEF does not execute) on the managed lake and on every enabled Direct Query engine; every error code introduced in this phase tested; receipts on every result; evidence verification proven by tests that corrupt data; performance baseline recorded; Owner demo.

---

## P3 — Access and security → `v0.4.0-alpha.1`

**Goal:** server mode with authentication, roles, groups, row and column security, audit and a permission-safe cache.
**Required reading:** module packs `access.md`, `semantic.md` (row "P3"), `server.md`, `conformance.md`.

| ID | Issue | Module |
|---|---|---|
| P3-01 | Normative cases: security scenarios (`conformance/security/scenarios/`) for pages, page-bound queries, rows, columns, masking, cache, export, possible values, MCP and missing attributes, plus the Direct Query cases of ADR-0027 §6 (callers with no mapping under allow- and deny-lists, registered deny-list callers with nothing blocked, cap, `NULL`, mapping tables outside the managed lake, views and macros, files, symbolic links and hard links reaching outside the data location, files of Route A file sources and of other projects in the instance, locations registered later inside an existing lake, catalog files replaced by links, catalog roles that read outside their schema, overlapping locations, reused snapshot ids, revoked mapping or registration in cache) *(Owner merges)* | conformance |
| P3-02 | Users, project memberships, roles and the instance administrator (SQLite locally, PostgreSQL schema in server mode) | access |
| P3-03 | `audit-event` contract v1; append-only audit log with hash chain; configurable retention | access |
| P3-04 | Local authentication (`hashlib.scrypt`), server-side sessions, cookie and `Origin` rules | access |
| P3-05 | OIDC (Authorization Code + PKCE, `response_mode=query`, identity by issuer + subject), groups and explicit group mapping | access |
| P3-06 | Real `security-context` provider and permission fingerprint | access |
| P3-07 | Row policies: evaluation and placement before aggregation on every path | semantic |
| P3-08 | Column policies: visible, masked, hidden, with the masking rules of `security.md` §3.3 | semantic |
| P3-09 | Fail-closed on missing attributes: zero rows, `POLICY_ATTRIBUTE_MISSING` notice, audit event | semantic |
| P3-10 | Result cache keyed by permission fingerprint; invalidation by run records across workers (PostgreSQL `LISTEN`/`NOTIFY` in server mode); per-dataset settings; for datasets outside the managed lake, the key of ADR-0027 §7 with mapping- and registration-table snapshots checked on every lookup | semantic |
| P3-11 | Role enforcement on every HTTP route; per-role query limits and asynchronous queries | server |
| P3-12 | Administration API and CLI: users (instance administrator), memberships and groups (project Builder) | server |
| P3-13 | Security conformance runner over the scenarios, as a required check, on the managed lake and on every enabled Direct Query engine (ADR-0027 §6) | conformance |
| P3-14 | Property-based non-broadening tests | semantic |
| P3-15 | Transformation worker started and supervised by the server in server mode (restart after a crash, stop with the server, status through the API), on the P1-21 scheduler (decision #133) | server |

**Exit criteria:** every security scenario passes, or is `expected-fail` linked to the later issue that implements it (pages P6, exports P6–P7, MCP P9); OIDC verified against a real identity provider in CI; threat models recorded; the cache never crosses permission fingerprints.

---

## P4 — Semantic engine: S1, S3, S6 → `v0.5.0-alpha.1`

**Required reading:** module pack `semantic.md` (row "P4").

| ID | Issue | Module |
|---|---|---|
| P4-01 | Normative cases for S1 (from `CASE-101`), including non-broadening cases *(Owner merges)* | conformance |
| P4-02 | Context modifiers `all`, `all_selected`, `keep`, `remove` | semantic |
| P4-03 | Level of detail `fixed`, `include`, `exclude` | semantic |
| P4-04 | Calculation groups | semantic |
| P4-05 | Normative cases for S3 *(Owner merges)* | conformance |
| P4-06 | Calendars (Gregorian, fiscal, business tables) and period coverage rules | semantic |
| P4-07 | Period operators: previous period, same period last year, to-date, rolling, cumulative | semantic |
| P4-08 | Normative cases for S6 *(Owner merges)* | conformance |
| P4-09 | Human-readable receipt (i18n), security filters described safely | semantic |
| P4-10 | Result comparison: model, data, filter or policy change | semantic |
| P4-11 | Cost estimation, budgets before execution and "warn before waiting" (law 7, `QUERY_BUDGET_EXCEEDED`) | semantic |
| P4-12 | Controlled deprecation of metrics (law 8, `DEPRECATED_METRIC`, `METRIC_IN_USE`) and drill paths | semantic |

**Exit criteria:** all new cases green; corpus still 21/21; non-broadening properties hold for every modifier.

---

## P5 — Semantic engine: S2, S4, S5 → `v0.6.0-alpha.1`

**Required reading:** module pack `semantic.md` (row "P5"); `transform.md` for pre-aggregation refresh; `server.md` and ADR-0024 for the project writer.

| ID | Issue | Module |
|---|---|---|
| P5-01 | Normative cases for S2, with security scenarios for possible values *(Owner merges)* | conformance |
| P5-02 | Named analysis contexts and comparison | semantic |
| P5-03 | Possible-values API over authorized rows only | semantic |
| P5-04 | Normative cases for S4 and S5 *(Owner merges)* | conformance |
| P5-05 | Metric lifecycle and validation API: draft, certified, deprecated | semantic |
| P5-06 | Metric preview with the author's permissions and impact on other metrics (dashboard impact arrives in P6-08) | semantic |
| P5-07 | Project writer (ADR-0024): atomic text saves, text-span form edits with diff preview and re-parse verification, conflict detection, commits with the user's identity in server mode, audited | server |
| P5-08 | Author proposals, Builder certification, publishing to the Ossie file through the project writer (ADR-0024) | semantic |
| P5-09 | `preaggregation` contract v1 and definitions generated from the declared pre-aggregations | semantic |
| P5-10 | Refresh of pre-aggregation definitions by `transform` (`transformation.md` §8) | transform |
| P5-11 | Pre-aggregation matching with equivalence proof and policy-column check | semantic |
| P5-12 | Pre-aggregation suggestions from query statistics | semantic |

**Exit criteria:** all S1–S6 capabilities implemented with cases; pre-aggregations never change results.

---

## P6 — Web foundation and dashboards → `v0.7.0-alpha.1`

**Required reading:** module packs `web.md` (row "P6"), `dashboards.md`.

| ID | Issue | Module |
|---|---|---|
| P6-01 | Design tokens, typography, light and dark themes, validated chart palette | web |
| P6-02 | Application shell, navigation, sidebar, project switcher, command palette and shortcuts | web |
| P6-03 | Internationalization (es default, en; `es-BO` formats) and exact-number formatting from strings | web |
| P6-04 | Authentication screens (local and OIDC) | web |
| P6-05 | `dashboard` contract v1; dashboards module: load and validate | dashboards |
| P6-06 | `visual-spec` contract v1 (reserves `python` with `UNSUPPORTED_VISUAL_TYPE`) and validation | dashboards |
| P6-07 | Drafts and publishing through the project writer; page access decisions (`PAGE_FORBIDDEN`); dashboards HTTP endpoints | dashboards |
| P6-08 | Page-bound query validation (`security.md` §3.2) and metric impact on dashboards, pages and visuals | dashboards |
| P6-09 | Dashboard canvas: Gridstack isolated in one component; positions in Orvex format | web |
| P6-10 | Chart renderer: ECharts with function-free JSON allow-list; bar, line, area, combo | web |
| P6-11 | Scatter, heatmap, funnel, pie/donut; maps from project GeoJSON (choropleth and points) | web |
| P6-12 | KPI card (comparison, sparkline) and detail table (server-side sorting and pagination) | web |
| P6-13 | Filters (list, search, range, relative dates, hierarchical) with possible values | web |
| P6-14 | Cross-filtering, drill-down and drill-through | web |
| P6-15 | Receipt panel ("Where does this come from?") and freshness badges | web |
| P6-16 | Progressive visual editor: basic, intermediate, advanced (law 6) | web |
| P6-17 | Responsive reflow and per-page `mobile_order` | web |
| P6-18 | Server-side export to CSV and Excel, audited, with declared losses (law 12) | server |
| P6-19 | Server-side export to PDF, audited | server |
| P6-20 | Explorer on the semantic model | web |
| P6-21 | End-to-end (both runtime modes on Linux) and accessibility checks for every route, as required checks | web |

**Exit criteria:** a manager can open a shared URL and navigate allowed pages on desktop and mobile; accessibility checks green; Owner demo.

---

## P7 — The matrix → `v0.8.0-alpha.1`

**Required reading:** module packs `web.md` (row "P7"), `semantic.md` (row "P7").

| ID | Issue | Module |
|---|---|---|
| P7-01 | Normative cases for the matrix: totals, ratios, distinct counts, expand leakage, `NULL` vs 0 *(Owner merges)* | conformance |
| P7-02 | `matrix` contract v1 (`MatrixDefinition`, `MatrixQuery`, `MatrixResult`, "show value as" options) | semantic |
| P7-03 | Matrix planning: grouping sets, totals policy, recomputation at every level, "show value as" computed by the engine | semantic |
| P7-04 | Deterministic viewport pagination, width and cell limits (`MATRIX_TOO_WIDE`), matrix endpoint with page-bound matrix queries built by `dashboards` | semantic |
| P7-05 | Matrix component: WAI-ARIA grid with virtualization on both axes | web |
| P7-06 | Hierarchies, expand/collapse, stepped and tabular layouts | web |
| P7-07 | Subtotals (toggle, top/bottom), grand totals, sorting per level, frozen headers, column resize | web |
| P7-08 | Rendering of "show value as" and time-intelligence columns returned by the engine | web |
| P7-09 | Conditional formatting (color scales, data bars, icons, rules) and sparklines | web |
| P7-10 | Drill and cross-filter from the matrix; receipt on every cell including totals | web |
| P7-11 | Excel export preserving hierarchy, subtotals and formats | server |
| P7-12 | Performance benchmark and optimization on large matrices | web |

**Exit criteria:** the eight correctness rules of `frontend.md` §4.2 proven by tests; keyboard and screen-reader walkthrough; performance targets met; Owner demo.

---

## P8 — Studio → `v0.9.0-alpha.1`

**Required reading:** module pack `web.md` (row "P8"), `server.md`.

| ID | Issue | Module |
|---|---|---|
| P8-01 | Project file editing API over the project writer: open, validate, save, conflict resolution data, uncommitted-changes status (local mode) | server |
| P8-02 | Monaco integration with server-side validation markers | web |
| P8-03 | Project file browser and model view (SQL and YAML tabs) | web |
| P8-04 | Run models from the UI; run history and logs | web |
| P8-05 | `lineage-graph` contract v1 and endpoint: `server` composes the transform graph, the semantic model and dashboard references; layered layout | server |
| P8-06 | Lineage graph view with Svelte Flow | web |
| P8-07 | Metric authoring UI: draft, preview, impact, publish, proposals | web |
| P8-08 | Administration UI (users, groups, memberships, policies) and connections UI with secret references | web |
| P8-09 | Multi-project instance UI (create, register, **Save as** for in-memory projects) and query diagnostics for Builders | web |
| P8-10 | Living documentation of the semantic model generated from Ossie (law 8) | server |

**Exit criteria:** the Builder completes the whole flow (connect, model, define metrics, secure, publish a dashboard) from the UI or from files interchangeably; Owner demo.

---

## P9 — AI and operations → `v0.10.0-alpha.1` (MVP scope complete)

**Required reading:** module packs `mcp.md`, `server.md`, `repo.md`.

| ID | Issue | Module |
|---|---|---|
| P9-01 | `mcp-tools` contract v1 and tools `describe_model`, `query`, `explain`, `list_dashboards` (page decisions supplied by `server`) | mcp |
| P9-02 | Personal access tokens: create, store hashed, scope to one project, expire, revoke, audit | access |
| P9-03 | MCP transports (stdio locally, Streamable HTTP in server mode with personal access tokens) acting as the user; audit of every call (`security.md` §5) | mcp |
| P9-04 | Project `AGENTS.md` generator | server |
| P9-05 | Environment promotion (development → production) of project files and data snapshots (law 8) | server |
| P9-06 | OpenTelemetry metrics and traces; usage statistics per dashboard, metric and user | server |
| P9-07 | Alerts for run failures, reconciliation, staleness and degradation; channels: email (`smtplib`) and webhook | server |
| P9-08 | Official Docker image and server-mode compose with PostgreSQL | repo |
| P9-09 | Backup and restore of projects and instance state | server |
| P9-10 | CI matrix for local mode on Linux, Windows and macOS, as a required check | repo |
| P9-11 | Upgrade framework for module state (migrations with verification) | server |

**Exit criteria:** every item of `product/vision.md` §5 implemented; Owner demo; the product is feature-complete for v1.0.0.

---

## P10 — Beta: pilot and hardening → `v0.11.0-beta.1` … `beta.K`

`beta.1` is tagged when P9 closes (Owner approval required). Formats are frozen; dependency updates limited to patch, minor and security (`versioning.md` §5).
**Owner task:** onboard the pilot users (manager and one or two department heads) and collect feedback as `type:bug` issues.

| ID | Issue | Module |
|---|---|---|
| P10-01 | Measure every non-functional target (`vision.md` §6); open one issue per miss | repo |
| P10-02 | Full accessibility audit with manual screen-reader passes | web |
| P10-03 | Consolidated security review and threat model | repo |
| P10-04 | User documentation for Builders (Spanish and English) | repo |
| P10-05 | User documentation for Authors and Viewers (Spanish and English) | repo |
| P10-06 | Upgrade path from the last alpha and between betas | server |

## RC and GA → `v1.0.0-rc.K` → `v1.0.0`

- `v1.0.0-rc.1` is tagged when the beta has no open `priority:p0`/`p1` bugs and the release criteria are evidenced (Owner approval required). Only defect fixes between release candidates.
- **`v1.0.0`** is tagged after the Owner comments `Approved for release v1.0.0` on the release issue. From then on, SemVer guarantees the public contracts.

## After v1.0.0 (decided direction)

| Item | Module | Decision |
|---|---|---|
| Direct Query on a remote DuckDB through Quack: server-side read-only access and limits, verified HTTPS, version check; corpus and security suite green | store | ADR-0027 §10 (#107) |
| Direct Query on ClickHouse: ClickHouse dialect adapter (plan → ClickHouse SQL with bound parameters), execution with server-side limits, exact types, `EXPLAIN ESTIMATE`; corpus and security suite green with results identical to the reference | semantic, store | ADR-0027 §10 |
| Python models, ML predictions and Python visuals | transform, semantic, web | ADR-0023 |
