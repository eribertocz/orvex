# 0005 — Local zero-configuration mode and opt-in server mode

**Status:** Accepted (2026-10-26)

## Context
DuckDB's appeal is that typing `duckdb` gives a working database. The Owner wants Orvex to follow that principle while also supporting a shared server for managers and department heads.

## Decision
- `orvex` opens the UI on an in-memory project; `orvex <path>` opens or creates a persistent project. One process, no login, no Docker, local DuckLake catalog, local SQLite state.
- `orvex <path> --server` (or the official Docker image) enables multi-user operation: authentication, multiple HTTP workers, transformation workers, DuckLake with a PostgreSQL catalog, PostgreSQL module state.
- One instance hosts multiple projects; projects are created from the UI or the CLI.

## Alternatives considered
- Server-only (Metabase-style): heavier first experience.
- Desktop-only: cannot share by URL.

## Consequences
Every module has adapters for both modes, selected at composition. Both modes run in CI on Linux from P0 (module tests) and P6 (end-to-end); local mode runs on Linux, Windows and macOS from P9 (P9-10). State locations per mode are listed in `architecture/overview.md` §4.3.
