# Laws, anti-patterns and design principles

**Origin:** the Owner's "Fundamental Laws of a BI Tool" (2026-10-11), as refined in the bi-platform project, adopted and adapted for Orvex.

These rules are **non-negotiable** and **retroactive**: anything already built is audited against them, and a violation is a defect regardless of when it was introduced.

**Priority:** laws (§1) > anti-patterns (§2) > design principles (§3). Within the laws, laws **1, 2, 3 and 5** rank above the rest. If two laws conflict, the Owner decides through a new ADR.

**Rule for every pull request:** the PR description lists which laws it touches and how it complies. A PR that violates a law is not merged, even with green CI.

---

## 1. Laws

### Law 1 — Single source of truth

Every displayed number is traceable to one semantic definition that is unique, versioned and audited.

- The semantic engine is the only source of metrics, dimensions and relationships.
- No business calculation happens in the frontend.
- Every metric has a canonical name, description, formula, owner and history.
- Business users never write raw SQL for metrics.
- **Totals and subtotals are computed by the engine**, never by summing rows on screen. Summing a non-additive metric (for example distinct customers across regions) produces a false total. Every total is requested from the engine as part of the query, and the engine decides whether to sum or recompute.
- The frontend only performs presentation transforms: formatting, visual ordering and scaling.

### Law 2 — Transparency and auditability

Anyone with permission can answer "where does this number come from, and why?".

- The user can see the definition, the filters (including security filters, described without revealing protected values) and the data timestamp.
- The generated SQL is available but secondary; the explanation is given first in semantic terms.
- Lineage is available from source to model to metric to visual, down to the column.
- Model changes are versioned and reversible (Git).
- Every query is recorded: who asked what, and when.
- **The receipt.** Every result carries a receipt containing the canonical semantic query, the semantic model version, the plan fingerprint and the data origin (the DuckLake snapshot read, or the live source and read instant). The receipt declares its reproducibility: **exact** while every required snapshot is retained, or **explainable only** when a snapshot expired or the read was live.

### Law 3 — Security by design

Security is applied before data leaves the engine — never only in the frontend.

- Row-level security (RLS) and column-level security (CLS) are enforced inside the semantic engine.
- Roles, groups and granular permissions follow least privilege and deny by default.
- Single sign-on via OIDC.
- **Single enforcement point:** API, dashboards, matrix, explorer, cache, export and AI all go through the semantic engine with the caller's security context. No path executes SQL outside it.
- No user filter can remove a mandatory policy (proven by property-based tests).
- **Advanced chart options are JSON only.** Apache ECharts options can contain JavaScript functions and HTML (for example `formatter`), which is a cross-site scripting risk. Only function-free JSON validated against an allow-list of properties is accepted; rich text is sanitized; formats and templates are declarative.

### Law 4 — Consistency

The same number is the same number everywhere, within the declared freshness.

- Same metric, same filters, same data ⇒ same result (deterministic engine, canonical fingerprints).
- Freshness is visible on every panel.
- Transformation models are deterministic and reproducible.
- Caching is semantic, never raw SQL caching.
- Time travel is available through DuckLake snapshots.
- **The cache key** includes the canonical query, the semantic model version, the data version **and the fingerprint of the caller's permission decision** (policy version and the attributes that took part). A user identifier alone is not enough: after a policy or attribute change (a revocation), the fingerprint changes and cached results stop being served.

### Law 5 — Layer separation

The frontend never knows the physical structure of tables.

| Layer | In Orvex |
|---|---|
| Ingestion / transformation | `transform` module: SQL models + YAML sidecar |
| Storage | `store` module: DuckLake (DuckDB engine); execution on Direct Query engines (ADR-0027) |
| Semantic engine | `semantic` module: Apache Ossie in, typed results and receipts out |
| Query API | OpenAPI with exact typed values |
| Frontend | SvelteKit, Svelte 5, ECharts, own matrix component |

- Two audiences: the **Builder's studio** shows tables and files deliberately; **consumption** surfaces (dashboards, explorer, API, AI) know only the semantic model.
- CI enforces the module boundaries.

### Law 6 — Progressive levels of experience

Serves business users and advanced analysts without sacrificing either.

- **Basic:** metrics, dimensions and filters from the model; the visualization cannot be broken.
- **Intermediate:** axes, series, chart types, colors and tooltips within a validated subset.
- **Advanced:** almost the full ECharts option object, JSON only and validated (law 3); data arrives already filtered.
- The three levels are views of the **same** visual contract. Switching to a lower level never rewrites or silently drops options it cannot edit: it flags them and keeps them intact.
- The same idea applies across the product: canvas or code for models, form or YAML for metrics.

### Law 7 — Predictable performance

Users know what to expect.

- Timeouts, cancellation and admission control per resource.
- Limits per user or role.
- Cost estimation before execution.
- Semantic cache (law 4).
- Asynchronous queries and materializations.
- Clear loading, progress and error states on every screen.
- **Warn before waiting:** if the estimate exceeds the declared speed target, the system warns before executing and proposes an alternative (for example "materializing this aggregate would bring it to 0.3 s").

### Law 8 — Governance and lifecycle of the model

The semantic model is code.

- Versioned in Git and reviewed by pull request.
- Environments (development, staging, production) as DuckLake-backed branches without copying data, when the storage supports it.
- Metric tests: expected values and transformation audits.
- Living documentation generated from Ossie.
- **Controlled deprecation:** a metric is marked `deprecated` with a removal date and a replacement; the engine warns whoever queries it; usages are listed; it cannot be deleted while in use.

### Law 9 — Observability

What is not measured cannot be operated.

- Structured logs with a request identifier; OpenTelemetry metrics.
- Usage per dashboard, metric and user.
- Latency p50, p95 and p99.
- Cache hit rate.
- Degradation alerts.
- **Reproduce from the log:** the log of each query stores its receipt, not raw SQL. Because the engine is deterministic, a problematic query can be reproduced exactly with the same model and data **if the receipt declares exact reproducibility** and the data is still retained.

### Law 10 — Controlled extensibility

Extend without breaking the core.

- Connectors are isolated adapters.
- New visualizations are added without touching the engine (renderer registry over the visual contract).
- Stable public API (versioned OpenAPI); the semantic engine is usable as a library and as a service.
- The advanced mode is isolated (law 3).

### Law 11 — Demonstrable exactness

- Exact decimals end to end: no JavaScript rounding, no `0.30000000000000004`. `DECIMAL` and `BIGINT` values travel as strings in JSON and are never converted to floating point for computation.
- Nulls follow their definition; `NULL` is always distinguishable from zero.
- Time zones are explicit for every timestamp; rounding only at presentation.
- If the engine cannot prove that a number is correct, it **rejects** it with a stable error code and a reason.

### Law 12 — Openness

- No lock-in: the model enters and leaves as Apache Ossie, data as Arrow and Parquet, definitions as files in Git.
- Every export declares what it loses.

### Law 13 — Accessibility

- WCAG 2.2 AA on every screen: keyboard, screen reader and contrast, verified with automated checks (axe) and manual keyboard and screen-reader passes on the main flows.

---

## 2. Anti-patterns (prohibited)

| # | Anti-pattern | Why it is prohibited |
|---|---|---|
| A1 | SQL in the frontend for business metrics | Bypasses the single source of truth and security |
| A2 | Metrics calculated only inside a chart | Produces inconsistent numbers across dashboards |
| A3 | Security only in the frontend | Trivially bypassed by calling the API |
| A4 | God dashboard (monolithic, unstructured) | Unmaintainable and slow |
| A5 | Magic configuration (metrics defined only in the UI, unversioned) | Not reviewable, not reproducible |
| A6 | Silent failures (returning 0 or null without explanation) | Users trust wrong numbers |
| A7 | Over-fetching (bringing all data to the client to filter) | Breaks performance and security |
| A8 | Frontend coupled to physical tables | Violates layer separation |
| A9 | Binary permissions (only admin vs viewer) | Cannot express real organizations |
| A10 | Ignoring freshness (showing data without its update time) | Users decide on stale data unknowingly |
| A11 | Advanced mode bypassing the engine or security | Reopens every hole the engine closes |
| A12 | Destructive changes without control (deleting metrics in use) | Breaks dashboards and trust |

---

## 3. Design principles

| # | Principle | Meaning |
|---|---|---|
| P1 | Progressive disclosure | Show complexity only when the user asks for it |
| P2 | Fail fast, fail loud | When something is wrong, fail clearly and visibly; never return wrong numbers silently |
| P3 | Semantics first, visualization second | Resolve the meaning of the data before deciding how to draw it |
| P4 | Least privilege by default | Every user starts with the minimum access |
| P5 | Determinism | Same definition + same filters + same data = same result |
| P6 | Human-readable semantics | Metrics are understandable by business people |
| P7 | Separation of concerns | Business logic ≠ presentation logic ≠ storage logic |
| P8 | Immutability of historical data | Historical data is never rewritten without a trace |
| P9 | Observability by default | If it cannot be measured, it cannot be operated |
| P10 | Extensibility without corruption | Extending never breaks the laws or the semantic model |

## 4. Quick acceptance check for any feature

A feature is acceptable only if:

1. it violates no law;
2. it introduces no anti-pattern;
3. it follows the design principles, or its issue documents why a principle does not apply.
