# Product vision

## 1. Problem

Organizations that analyze data with Power BI or Excel face four recurring problems:

1. **Sharing is expensive.** Sharing a report requires paid licenses for every viewer.
2. **Logic is locked in.** Business logic lives in proprietary languages (for example DAX) and cannot move to another tool.
3. **Numbers cannot be explained.** Nobody can answer "where does this number come from?" without reverse-engineering a report.
4. **Numbers are silently wrong.** Non-additive metrics are summed, joins duplicate amounts (fan-out), and totals are computed from visible cells.

Existing open-source tools solve parts of this, but either feel dated, lock logic into their own formats, or do not prove the correctness of the numbers they show.

## 2. Product statement

Orvex is a self-hosted BI platform where:

- data is transformed with **plain SQL**;
- business meaning is defined once in **Apache Ossie**, the open semantic standard;
- a **semantic engine** compiles every question into correct SQL — or rejects it with a stable, explainable error;
- dashboards, matrices, exports and AI agents consume **only** that semantic engine;
- every number carries a **receipt** that explains it end to end;
- getting started is **as easy as DuckDB**: one command opens a working project.

## 3. Users and roles

| Role | Typical person | Can do |
|---|---|---|
| **Builder** | Data engineer (the Owner) | Everything in a project: connections, models, semantic model, policies, memberships and groups, dashboards. Users themselves are managed by the instance administrator (also the Owner) |
| **Author** | Department heads | Create and edit dashboards from certified metrics and dimensions; no SQL, no new metrics |
| **Viewer** | General manager, staff | View published dashboards and pages they are allowed to see |

Rules:

- Roles are assigned **per project**. One person can be Author in one project and Viewer in another.
- Users are **global to the instance**. Groups are **per project** and decide page access.
- Only the Builder creates metrics. Authors may **propose** a metric from the UI; the Builder approves or rejects it (capability S4).

## 4. Core flow (end to end)

1. **Connect** to data along two routes (ADR-0027): raw sources (Parquet, CSV, DuckDB, SQLite, PostgreSQL, and ClickHouse by extraction) to transform, or an existing DuckLake to query in place, without copying.
2. **Transform** raw sources with SQL models (full or incremental) into clean facts and dimensions stored in **DuckLake**; data queried in place skips this step.
3. **Model meaning** in Apache Ossie: datasets, dimensions, metrics, relationships, plus Orvex extensions (additivity, grain, units, time roles, policies, cache).
4. **Ask**: the semantic engine receives a semantic query, applies security, proves correctness, plans, generates the SQL of the dataset's engine, executes it there and returns typed results with a receipt.
5. **Present**: dashboards (pages, charts, KPIs, tables, the matrix, filters) built by Builders and Authors.
6. **Share**: a URL per dashboard; access by role, group (pages), row and column policies.
7. **Explain**: any number can be traced to its definition, filters, policies, data version and source rows' lineage.
8. **Automate**: incremental loads from production (read-only), driven by declared freshness (for example every 10 minutes).
9. **Access by AI**: agents query the semantic model through MCP with the same permissions as the user.

## 5. MVP scope (released as v1.0.0 stable)

Everything below is **in scope**. Nothing is simplified to ship earlier; capabilities are designed completely and built in the order defined in [`../roadmap.md`](../roadmap.md).

| Area | In scope |
|---|---|
| Runtime | Local single-user mode (`orvex`, `orvex <project>`) and server mode (`orvex <project> --server`); official Docker image |
| Sources | Route A (transformed into DuckLake): Parquet, CSV, DuckDB, SQLite, PostgreSQL, ClickHouse by extraction. Route B (Direct Query): an existing DuckLake |
| Warehouse | DuckLake; catalog in a DuckDB file (local-mode default), SQLite, or PostgreSQL (server-mode default; on premises or in the cloud); data files on local disk, a network file system or object storage (ADR-0027 §2) |
| Transformation | Pure SQL models with YAML sidecar; full, incremental (append, merge, delete+insert, partition overwrite); automatic incremental filter; lookback; scheduled full refresh; freshness-driven scheduling; audits with write-audit-publish; OpenLineage events |
| Semantic engine | Full parity with SEF (all 21 corpus cases) plus capabilities S1–S6 (see [`../architecture/semantic-engine.md`](../architecture/semantic-engine.md)); verified evidence; receipts; caching |
| Security | Local users and OIDC single sign-on; roles; groups; page-level, row-level and column-level security; audit log |
| Presentation | Dashboards with pages and sidebar; charts (Apache ECharts); KPIs; detail table; **matrix (pivot)**; filters; cross-filtering; drill-down and drill-through; export (CSV, Excel, PDF) |
| Studio | SQL/YAML editing (Monaco), lineage graph (Svelte Flow), explorer, metric authoring and impact preview |
| AI | MCP server exposing the semantic model; `AGENTS.md` generated in every project |
| Operations | Structured JSON logs; OpenTelemetry metrics; configurable retention for DuckLake snapshots and audit log |
| Experience | Spanish (default) and English UI; `es-BO` number and date formats; light and dark mode; keyboard-first; command palette (⌘K); WCAG 2.2 AA |

**Out of scope for v1.0.0** (each is a candidate for a later version, decided by the Owner): SAML/SCIM, multi-tenant hosting, real-time streaming sources, write-back to sources, a hosted cloud service, Direct Query on ClickHouse and on a remote DuckDB through Quack (direction decided in ADR-0027; ClickHouse is reached by extraction in v1.0.0), and **Python models, machine-learning predictions and Python visuals** (Polars, Arrow, DuckDB, XGBoost, LightGBM, scikit-learn, Plotly, Matplotlib). The latter already has a design direction (ADR-0023) and reserved contract values, so adding it after v1.0.0 requires no redesign.

## 6. Non-functional requirements

All targets are measured with the reference dataset (synthetic, same shape as the Owner's real data) on the reference machine. A target is met only when measured, never estimated.

| Requirement | Target |
|---|---|
| Fact table volume | Up to 100 million rows per fact table |
| Chart query, no cache (p95) | < 2.0 s |
| Chart query, cache hit (p95) | < 0.3 s |
| Dashboard page load (p95) | < 3.0 s on desktop and mobile |
| Matrix first viewport (p95), 1 M source rows, 3 row levels × 2 column levels, totals on | < 2.0 s without cache |
| Matrix expand or scroll to a new block (p95) | < 1.0 s |
| Concurrent users | 30 |
| Freshness | Configurable per model; minimum supported interval 5 minutes |
| Reference machine | 16 GB RAM, SSD, 4 cores |
| Supported platforms | Server: Linux (Docker). Local mode: Linux, Windows, macOS |
| Accessibility | WCAG 2.2 AA |

## 7. Product principles specific to Orvex

These complement the laws in [`laws.md`](laws.md):

1. **Quality before speed.** No design is simplified to ship earlier. Order of construction is the only lever.
2. **As easy as DuckDB.** The default path needs no configuration; complexity is opt-in.
3. **Open standards first.** SQL, Apache Ossie, Apache Arrow, Parquet, DuckLake, OpenAPI, JSON Schema, OpenLineage, OpenTelemetry, MCP.
4. **Rescue the best, fix the worst.** Every capability is designed after studying 2–4 reference tools; the issue or ADR records what is adopted, what is rejected, and what is improved.
5. **Replaceable modules.** Any module can be rewritten (for example in Rust) or reused alone without affecting the others.
