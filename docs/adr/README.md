# Architecture Decision Records

One decision per file: `NNNN-kebab-title.md`. Each record contains **Status, Context, Decision, Alternatives considered, Consequences**. Product and architecture decisions require Owner approval. An ADR is never edited after acceptance except to change its status; a new ADR supersedes it explicitly.

Records 0001–0026 were approved by the Owner during the design session of 2026-10-26; later records state their own date.

| # | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-dependency-selection-policy.md) | Open standards first; dependency selection policy | Accepted |
| [0003](0003-python-backend-and-svelte-web.md) | Python backend with FastAPI; SvelteKit web | Accepted |
| [0004](0004-modules-contracts-and-independent-versions.md) | Modules with contracts, own state and independent versions | Accepted |
| [0005](0005-local-and-server-runtime-modes.md) | Local zero-configuration mode and opt-in server mode | Accepted; amended in part by 0027 |
| [0006](0006-duckdb-and-ducklake.md) | DuckDB as the only analytical engine; DuckLake as the warehouse | Accepted; superseded in part by 0027 (more after v1.0.0) |
| [0007](0007-projects-as-files.md) | Projects as files in Git; module state separate | Accepted |
| [0008](0008-sql-models-with-yaml-sidecar.md) | Transformation models: pure SQL plus YAML sidecar | Accepted |
| [0009](0009-apache-ossie-native-format.md) | Apache Ossie as the native semantic format with `ORVEX` extensions | Accepted |
| [0010](0010-semantic-engine-scope.md) | Semantic engine scope: SEF parity plus S1–S6; normative corpus | Accepted |
| [0011](0011-security-model.md) | Security model: roles, groups, row and column policies, own evaluator | Accepted |
| [0012](0012-result-cache.md) | Result cache keyed by permission fingerprint, invalidated by runs | Accepted; amended in part by 0027 |
| [0013](0013-dashboards-as-yaml.md) | Dashboards as YAML validated by JSON Schema | Accepted |
| [0014](0014-frontend-stack-and-design-system.md) | Frontend stack, design system and own matrix component | Accepted |
| [0015](0015-traceability.md) | Receipts, OpenLineage, run records and write–audit–publish | Accepted |
| [0016](0016-ai-access-through-mcp.md) | AI access through MCP | Accepted |
| [0017](0017-versioning-and-mvp-stable.md) | Semantic Versioning; the MVP is v1.0.0 stable | Accepted |
| [0018](0018-agent-roles-and-git-workflow.md) | Agent roles (one agent that specifies and implements, independent Reviewer), GitHub identity and Git workflow | Accepted |
| [0019](0019-languages.md) | English in the repository; Spanish-default internationalized UI | Accepted |
| [0020](0020-identity-license-and-data.md) | Renameable identity, Apache-2.0, synthetic data only | Accepted |
| [0021](0021-observability.md) | JSON logs and OpenTelemetry | Accepted |
| [0022](0022-latest-stable-dependencies.md) | Always the latest stable versions of dependencies | Accepted |
| [0023](0023-python-models-and-ml-extension-path.md) | Extension path for Python models, ML predictions and Python visuals (after v1.0.0) | Accepted (direction only) |
| [0024](0024-project-writer.md) | Project writer: code editors save text as typed; forms only for dashboards and metrics | Accepted |
| [0025](0025-lazy-relations-no-python-materialization.md) | Data stays in DuckDB: lazy relations, no materialization in Python | Accepted; rule 6 applied to row-policy values by 0027; rule 1 to be amended in part after v1.0.0 |
| [0026](0026-one-dialect-per-engine.md) | One SQL dialect per engine; no transpilation (no SQLGlot) | Accepted; rules 1, 3 and 4 to be superseded in part by 0027 after v1.0.0 |
| [0027](0027-two-data-routes.md) | Two data routes: managed DuckLake and Direct Query, one semantic engine | Accepted (Direct Query on Quack and ClickHouse: direction only, after v1.0.0) |
