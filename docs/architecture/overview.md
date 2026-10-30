# Architecture overview

## 1. Principles

1. **Modules with a single responsibility**, communicating only through language-neutral contracts. Any module can be rewritten (for example in Rust) or reused alone.
2. **The semantic engine is the core** and the single enforcement point for data access.
3. **As easy as DuckDB.** Local mode is one process with zero configuration; server mode is opt-in.
4. **Open standards** at every boundary.

## 2. Modules

| Module | Distribution | Single responsibility | Receives | Returns | Owns state |
|---|---|---|---|---|---|
| **store** | `orvex-store` | Store and read analytical data; execute queries on Direct Query engines (ADR-0027) | SQL plans, write batches, snapshot requests | Arrow results, snapshot identifiers | DuckLake catalog and Parquet files |
| **transform** | `orvex-transform` | Turn sources into clean tables | Connections, SQL models, model configs | Tables in the store, run records, OpenLineage events, audit results | Run history, incremental watermarks |
| **semantic** | `orvex-semantic` | Turn meaning into correct results | Ossie model, semantic query, security context | Typed results, receipts, stable errors, explanations, pre-aggregation definitions | Compiled-model cache, result cache, metric proposals, query statistics, evidence-verification results (own schema, §4.3) |
| **access** | `orvex-access` | Know who the caller is and what they may do | Credentials, OIDC tokens, personal access tokens | Security context (user, roles, groups, attributes, permission fingerprint) | Users, sessions, memberships, personal access tokens (hashed), audit log |
| **dashboards** | `orvex-dashboards` | Manage dashboard definitions and decide page access | Dashboard and page YAML, security context | Validated definitions, page access decisions | Dashboard files (in the project), drafts |
| **mcp** | `orvex-mcp` | Expose the semantic model to AI agents | MCP requests | MCP responses | None |
| **server** | `orvex` | Compose modules; serve HTTP and the CLI; write project files (ADR-0024) | HTTP requests, CLI commands | HTTP responses, static web assets | Instance configuration, project registry |
| **web** | `web/` (SvelteKit) | User interface | Public HTTP API only | — | None (per-viewer UI preferences only) |

Rules:

- `server` is the **only** module that knows the others. It composes them at startup (the composition root).
- `semantic` does not know that `access` exists: it receives a security context conforming to `contracts/security-context`.
- `web` knows only the `http-api` contract. Other contracts reach the browser only as schemas embedded in `http-api`.
- Project files are written only by `server`'s project writer (ADR-0024); other modules read them.
- Each module has its own version (`process/versioning.md`).

## 3. Contracts

All contracts live in `contracts/`, versioned by directory (`contracts/<name>/v1/`). Types in every language are generated from them.

| Contract | Format | Producer → consumer |
|---|---|---|
| `project` | JSON Schema for project files (`project.yaml`, `connections.yaml`); each module defines its own section by extending it | files → server → transform, store, mcp |
| `model-config` | JSON Schema for transformation model sidecars | files → transform |
| `store` | Python protocol + JSON Schema of its requests and results (plans/SQL with bound parameters, write batches, transactions, snapshots) | store → transform, semantic |
| `run-record` | JSON Schema | transform → server, semantic (cache invalidation, freshness) |
| `ossie-extensions` | JSON Schema for the `ORVEX` vendor payload in Ossie `custom_extensions` | files → semantic |
| `catalog` | JSON Schema: the semantic model as a caller may see it (metrics, dimensions, descriptions, `ai_context`, certification), already filtered by column policies | semantic → server, dashboards, mcp |
| `semantic-query` | JSON Schema | server, mcp, dashboards → semantic |
| `semantic-result` | Apache Arrow schema + JSON envelope (exact values as strings) | semantic → server, mcp |
| `receipt` | JSON Schema | semantic → server, mcp, audit |
| `semantic-error` | JSON Schema with stable codes | semantic → all consumers |
| `security-context` | JSON Schema | access → semantic, dashboards, mcp |
| `security-policies` | JSON Schema for `security.yaml` | files → semantic |
| `dashboard` | JSON Schema for dashboard and page YAML | files ↔ dashboards → server; server → mcp (page list already filtered by access) |
| `visual-spec` | JSON Schema for charts, KPIs, tables and the matrix | dashboards → server |
| `matrix` | JSON Schema for matrix query and result | server → semantic → server |
| `preaggregation` | JSON Schema: pre-aggregation definitions (target table, DuckDB SQL, base tables, refresh strategy) | semantic → server → transform |
| `lineage-graph` | JSON Schema: nodes and edges from sources to visuals with a layered layout | server → web (through `http-api`) |
| `audit-event` | JSON Schema for audit records (hash chain) | every module → access (audit log) |
| `mcp-tools` | MCP tool definitions (names, JSON Schema inputs and outputs) | mcp → AI clients |
| `http-api` | OpenAPI 3.1, embedding the schemas above that the browser needs | server → web, external clients |

External standards used at boundaries without a Orvex contract of their own: **OpenLineage** events (transform → lineage collectors) and **OpenTelemetry** OTLP (every module → collectors). Orvex follows them as published; it does not version them.

## 4. Runtime modes

### 4.1 Local mode (default)

```
orvex                    # in-memory project, opens the UI
orvex facturacion/       # opens or creates a persistent project
```

- One process runs `server`, every module in-process, and serves the web UI.
- DuckLake uses a **local DuckDB catalog** and local Parquet files inside the project by default. `project.yaml` can opt in to another location (ADR-0027 §2): a SQLite catalog, a PostgreSQL catalog on premises or in the cloud, and data files on a network file system or in object storage.
- Module state uses local SQLite files under the instance directory (§4.3).
- No login (a single implicit Builder), no Docker, and no external services by default.

### 4.2 Server mode

```
orvex facturacion/ --server     # or the official Docker image
```

- Processes: `server` (HTTP API + web) with multiple workers, and `transform` worker(s) for scheduled loads.
- DuckLake uses a **PostgreSQL catalog** by default, on premises or in the cloud, which supports concurrent readers and writers across processes and machines. A SQLite catalog serves a server on one machine. Data files live on local disk (one machine), a network file system or object storage (ADR-0027 §2).
- Module state uses PostgreSQL, **one schema and one database role per module**, so modules never read each other's tables.
- Login is required (local users and/or OIDC).
- Direct Query on an existing DuckLake is an optional connection executed by `store`, never a startup requirement; ClickHouse is reached only by Direct Query, in v1.0.0 (ADR-0028); Quack comes after v1.0.0 (ADR-0027).

Both modes run the same code; only the adapters selected at composition differ.

### 4.3 Where state lives

| State | Local mode | Server mode |
|---|---|---|
| Instance directory | `~/.<slug>/` (override with `<SLUG>_HOME`) | Configured data directory (a volume in Docker) |
| Module state (users, sessions, run history, audit log, drafts, metric proposals, query statistics) | One SQLite file per module in the instance directory | PostgreSQL, one schema and role per module |
| Project files | The project folder (a Git repository) | The project folder on the server |
| Warehouse | `<project>/.<slug>/lake/` (DuckLake, local DuckDB catalog) by default; any location of ADR-0027 §2 by opt-in | DuckLake with PostgreSQL catalog by default (SQLite on one machine); data files on disk, a network file system or object storage |
| In-memory project (`orvex` with no folder) | A temporary directory deleted on exit; **Save as** turns it into a project folder | Not available |
| Result cache | In-process | Per worker, invalidated across workers by PostgreSQL `LISTEN/NOTIFY` on each run record |
| Secrets | Environment variables referenced from `connections.yaml` | Environment variables or the platform's secret store |

## 5. Request flow (a dashboard chart)

1. The browser requests page `billing/arrears`. `server` authenticates the session through `access`, which returns the security context.
2. `server` asks `dashboards` whether the caller may see the page. Denied ⇒ `403` with code `PAGE_FORBIDDEN`.
3. For each visual (charts, tables and the matrix), the browser sends `{page, visual_id, interaction_state}` — never SQL and never a free-form query. `server` asks `dashboards` to check the page again and to build the semantic or matrix query from the published visual and the interaction state (`security.md` §3.2), then passes it to `semantic`.
4. `semantic` validates the query against the model, applies mandatory row and column policies from the security context, proves correctness, plans, checks the cache (key includes the permission fingerprint), generates the SQL of the dataset's engine with bound parameters and executes it through `store`: on a fixed DuckLake snapshot, of the managed lake or of an existing DuckLake (ADR-0027).
5. `semantic` returns a typed Arrow result, a receipt and freshness information.
6. The browser renders with ECharts or the matrix component. Exact values stay strings until formatting.

## 6. Project layout (what the Builder edits)

```
facturacion/                       # one project = one Git repository
├── project.yaml                   # name, version, default locale, retention settings
├── connections.yaml               # connection definitions; secrets are environment-variable references
├── models/
│   ├── staging/
│   │   ├── readings.sql           # pure SQL
│   │   └── readings.yaml          # optional sidecar: materialization, incremental, tests, freshness
│   └── marts/…
├── semantic/
│   └── facturacion.ossie.yaml     # Apache Ossie + ORVEX extensions
├── security.yaml                  # row and column policies (page groups live in page YAML)
├── assets/
│   └── geo/                       # GeoJSON files for maps (referenced by `geo_source`)
├── dashboards/
│   └── consumption/
│       ├── dashboard.yaml
│       └── pages/
│           ├── overview.yaml
│           └── arrears.yaml
├── AGENTS.md                      # generated: how AI agents should understand this project
└── .<slug>/                       # local warehouse and caches (ignored by Git)
    └── lake/                      # DuckLake catalog and Parquet files (local mode)
```

Files under the project are edited by hand or from the UI; UI writes go through the project writer and follow ADR-0024 (code editors save text exactly as typed; forms edit only dashboards and governed metrics, with a diff preview).

## 7. Repository layout (what the Agent builds)

```
orvex/
├── identity.toml                  # product name, single source
├── versions.toml                  # module versions per product release
├── contracts/                     # language-neutral contracts, versioned
├── packages/
│   ├── store/                     # orvex-store      → import orvex.store
│   ├── transform/                 # orvex-transform  → import orvex.transform
│   ├── semantic/                  # orvex-semantic   → import orvex.semantic
│   ├── access/                    # orvex-access     → import orvex.access
│   ├── dashboards/                # orvex-dashboards → import orvex.dashboards
│   ├── mcp/                       # orvex-mcp        → import orvex.mcp
│   └── server/                    # orvex (CLI + HTTP, composition root)
├── web/                           # SvelteKit application
├── conformance/
│   ├── semantic-corpus/           # normative semantic cases
│   └── security/                  # security conformance suite
├── tools/                         # rename script, synthetic data generator, boundary checker
├── docs/
└── .github/
```

Python packages share the `orvex` namespace through implicit namespace packages (PEP 420), so each module is installable and versioned on its own.

## 8. Technology

| Concern | Choice |
|---|---|
| Analytical engine | DuckDB (latest stable, ADR-0022); data stays in DuckDB as lazy relations, results leave as Arrow (ADR-0025). Direct Query: an existing DuckLake and ClickHouse in v1.0.0 (ADR-0028); a remote DuckDB through Quack after v1.0.0 (ADR-0027) |
| Warehouse format | DuckLake (DuckDB-file, SQLite or PostgreSQL catalog; data files on disk, a network file system or object storage; ADR-0027 §2) |
| Backend language | Python (latest stable) |
| HTTP | FastAPI (Pydantic included) |
| YAML | PyYAML |
| Semantic standard | Apache Ossie (pinned specification commit) |
| Results interchange | Apache Arrow |
| Lineage | OpenLineage |
| Telemetry | OpenTelemetry; JSON logs via the standard library |
| AI access | Model Context Protocol |
| Web | SvelteKit (static adapter), Svelte 5, TypeScript, Tailwind CSS v4 |
| Charts | Apache ECharts |
| Code editor | Monaco |
| Dashboard layout | Gridstack (isolated in one component) |
| Flow diagrams | Svelte Flow (xyflow) |
| Matrix | Own Svelte component (WAI-ARIA grid, virtualized) |
| Packaging | `pyproject.toml` standards (pip-compatible); Docker image for server mode |

Every runtime and library is used at its **latest stable release**, pinned exactly in lock files (ADR-0022). Rationale for each choice: [`../adr/`](../adr/README.md).
