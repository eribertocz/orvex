# 0002 — Open standards first; dependency selection policy

**Status:** Accepted (2026-10-26)

## Context
The Owner wants a tool that never locks users into one vendor and that stays maintained for years. Libraries with a single maintainer are a long-term risk.

## Decision
1. Prefer open standards at every boundary: SQL, Apache Ossie, Apache Arrow, Parquet, DuckLake, OpenAPI, JSON Schema, OpenLineage, OpenTelemetry, MCP, WAI-ARIA.
2. Runtime dependency selection order: (1) language or browser standard library; (2) DuckDB capabilities; (3) projects governed by a foundation, a large organization, or a funded team of at least three active maintainers (for example Svelte Flow by xyflow); (4) nothing that depends on a single maintainer.
3. Development-only tools may come from smaller projects if replaceable without touching product code.
4. Packaging uses Python standards (`pyproject.toml`) so the project works with `pip`; tool-specific features (for example of `uv`) are prohibited.
5. Exceptions are explicit and isolated: **Gridstack** (single main maintainer) is allowed only inside one component that stores positions in Orvex's own format, so it can be replaced without touching anything else.

Choices derived from this policy: DuckDB's own parser instead of SQLGlot; `hashlib.scrypt` instead of Argon2 libraries; PyYAML instead of ruamel.yaml; Monaco instead of CodeMirror; own matrix component; `clickhouse-connect` (ClickHouse, Inc.) for ClickHouse.

## Alternatives considered
- Choosing by popularity only: ignores governance risk.
- Writing everything in-house: reinvents well-governed standards.

## Consequences
Each new dependency requires a justification line in its pull request. Comment-preserving YAML rewriting is not taken from a library; the project writer edits only the text spans of changed nodes (ADR-0024).
