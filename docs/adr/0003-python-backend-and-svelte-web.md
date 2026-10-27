# 0003 — Python backend with FastAPI; SvelteKit web

**Status:** Accepted (2026-10-26)

## Context
The Owner's previous project (bi-platform) built the semantic engine in Rust; it is solid but slow to evolve. The Owner wants Orvex to reach a stable MVP in less time while surpassing that engine, and already uses FastAPI and DuckDB in production.

## Decision
- Backend in Python (latest stable release, ADR-0022) with FastAPI. Heavy computation runs in DuckDB; Python compiles, verifies and orchestrates.
- Web in SvelteKit with the static adapter, Svelte 5 and TypeScript, served by the Python server (one origin, no Node.js at runtime).
- Orvex and bi-platform are separate projects. Orvex reuses bi-platform's laws, process lessons and normative corpus, not its code.

## Alternatives considered
- Rust backend (bi-platform's choice): stronger static guarantees, much slower delivery for this team.
- Reusing SEF via bindings: mixes two languages from day one; kept as a future option behind the semantic contract (ADR-0004).

## Consequences
Rigor comes from architecture, strict typing (`mypy --strict`), immutable intermediate representations and conformance suites. A Rust implementation of any module remains possible later if it passes the same conformance tests.
