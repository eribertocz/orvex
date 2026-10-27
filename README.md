# Orvex

**Business intelligence built on open standards.** Orvex connects raw data, transforms it with plain SQL, models business meaning in [Apache Ossie](https://github.com/apache/ossie), and serves governed dashboards, matrices and AI access — with every number traceable to its source.

> **Status:** pre-alpha. This repository currently contains the project foundation (product laws, architecture, decisions, roadmap and process). No product code exists yet. See [`docs/roadmap.md`](docs/roadmap.md).

## Why Orvex

| Problem with incumbent BI tools | Orvex's answer |
|---|---|
| Sharing requires per-user licenses | Self-hosted, Apache-2.0, share by URL with roles |
| Business logic locked in proprietary languages (DAX, LookML) | Plain SQL for transformations, Apache Ossie YAML for semantics |
| Numbers that cannot be explained | Every number carries a **receipt**: definition, filters, policies, data version and lineage |
| Silent wrong totals (non-additive metrics, fan-out joins) | A semantic engine that **proves** correctness or rejects the query with a stable error |
| Heavy setup | `orvex` opens an in-memory project, like `duckdb`. Server mode is opt-in |

## Core technology

DuckDB · DuckLake · Apache Ossie · Python · FastAPI · SvelteKit · Svelte 5 · Tailwind CSS · Apache ECharts · Apache Arrow · OpenLineage · Model Context Protocol

## Documentation map

Start with [`docs/README.md`](docs/README.md). It defines which document governs which topic and the precedence order when two documents disagree.

| You want to… | Read |
|---|---|
| Understand what Orvex is and for whom | [`docs/product/vision.md`](docs/product/vision.md) |
| Know the non-negotiable rules | [`docs/product/laws.md`](docs/product/laws.md) |
| Understand the architecture | [`docs/architecture/overview.md`](docs/architecture/overview.md) |
| Understand the semantic engine (the core) | [`docs/architecture/semantic-engine.md`](docs/architecture/semantic-engine.md) |
| See why each decision was made | [`docs/adr/`](docs/adr/README.md) |
| See the plan, phases and versions | [`docs/roadmap.md`](docs/roadmap.md) |
| Contribute (humans or AI agents) | [`AGENTS.md`](AGENTS.md) and [`docs/process/`](docs/process/README.md) |
| Leer un resumen en español | [`docs/es/resumen.md`](docs/es/resumen.md) |

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
