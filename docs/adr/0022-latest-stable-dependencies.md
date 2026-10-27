# 0022 — Always the latest stable versions of dependencies

**Status:** Accepted (2026-10-26)

## Context
The Owner requires Orvex to always use the latest versions of DuckDB, DuckLake, FastAPI, Svelte, SvelteKit and every other library and runtime it uses. Unpinned versions break reproducibility; outdated versions accumulate security and compatibility debt.

## Decision
1. **Latest stable, always.** Every runtime, library and tool is used at its latest **stable** release (no alpha, beta, release-candidate, nightly or development builds in `main`). This applies to Python, Node.js (latest LTS line), DuckDB, DuckLake, FastAPI, Pydantic, PyYAML, OpenTelemetry, `clickhouse-connect`, Svelte, SvelteKit, TypeScript, Tailwind CSS, Apache ECharts, Monaco, Gridstack, Svelte Flow, test tools and linters.
2. **Exact pins.** Versions are pinned exactly in lock files so every build is reproducible; "latest" is achieved by updating the pins, never by floating ranges.
3. **Automatic updates.** GitHub Dependabot opens update pull requests weekly for Python, npm, GitHub Actions and Docker base images. Security updates are opened immediately.
4. **Updates pass the full gate.** An update merges only with green required checks, including the semantic corpus and security scenarios. Major-version updates (for example DuckDB 2 → 3) get their own issue with a migration note and are applied within the phase in which they are released, unless they break a law (the Owner decides) or the product is in beta or release candidate (then they wait until after `v1.0.0`, except security fixes).
5. **Phase start check.** At the start of each phase, the Agent verifies that no dependency is behind its latest stable release and opens update issues for any gap.
6. **Pre-releases are evaluated, not adopted.** A pre-release (for example the next DuckDB major) may run in a non-blocking experimental CI job; it is adopted only after its stable release.

## Alternatives considered
- Long-term-support versions only: more stable, but contradicts the Owner's requirement.
- Floating version ranges: non-reproducible builds.

## Consequences
Dependency updates are continuous small pull requests instead of rare large migrations. The conformance suites are the safety net that makes this policy safe.
