# 0004 — Modules with contracts, own state and independent versions

**Status:** Accepted (2026-10-26)

## Context
The Owner requires that each module has a single responsibility and a clear contract, can evolve without affecting others (for example rewriting the semantic engine in Rust), and can be reused alone (another team using only the transformation engine, the UI or the semantic engine).

## Decision
1. Modules: `store`, `transform`, `semantic`, `access`, `dashboards`, `mcp`, `server`, `web` (see `architecture/overview.md`).
2. A module never imports another module's internals. Communication uses contracts in `contracts/`, defined in language-neutral formats (JSON Schema, OpenAPI, Arrow schemas, SQL, YAML schemas).
3. `server` is the only composition root.
4. Each module owns its state; no shared tables. In server mode each module has its own PostgreSQL schema and database role.
5. Each module is usable as a library, a CLI and over HTTP where it applies.
6. Each module and each contract has its own version. Modules declare supported contract versions; the server refuses incompatible combinations. Breaking contract changes keep the previous version during a transition period defined in their ADR.
7. Conformance tests per contract decide whether an implementation can replace another.
8. Inside a module: `domain` / `application` / `adapters` layers with inward dependencies; abstractions only at contract boundaries.

## Alternatives considered
- Monolith without enforced boundaries: fast initially, impossible to replace parts later.
- Microservices per module: operational complexity contrary to the "as easy as DuckDB" principle; boundaries are enforced in code and CI instead.

## Consequences
More design effort at boundaries; CI checks imports and contract compatibility. Modules can be released, replaced and reused independently.
