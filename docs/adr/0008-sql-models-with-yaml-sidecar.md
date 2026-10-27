# 0008 — Transformation models: pure SQL plus YAML sidecar

**Status:** Accepted (2026-10-26)

## Context
Studied: Rill (simple `.sql` models; advanced models as YAML with SQL embedded as a string; `state` queries and `{{if incremental}}` templating), dbt (Jinja configuration inside SQL), SQLMesh (`MODEL (...)` header that is not valid SQL; `lookback`; automatic time filter). The Owner rejected mixing configuration and SQL in one file.

## Decision
- Each model is a pure `.sql` file (one SELECT, valid DuckDB SQL) plus an optional `.yaml` sidecar with the same stem.
- Configuration vocabulary aligned with dbt and Rill: `materialized`, `incremental.strategy` (`append`, `merge`, `delete_insert`, `partition_overwrite`), `unique_key`, `on_schema_change`, tests.
- The engine injects the incremental filter automatically when provably safe; otherwise the SQL uses the DuckDB 2.0 parameter `$since` (`filter: manual`). The same SQL serves full and incremental runs.
- Scheduling derives from declared `freshness`; periodic `full_refresh` captures source deletions.
- Dependencies are detected with DuckDB's own parser.
- Full specification: `architecture/transformation.md`.

## Alternatives considered
- SQLMesh-style header: one file, but not valid SQL and fragile for UI edits.
- Rill-style YAML with embedded SQL: loses SQL tooling; changing a model to incremental requires rewriting it.
- YAML front matter inside a SQL comment: valid SQL, but mixes concerns (rejected by the Owner).

## Consequences
Two files per configured model; mitigated by optional sidecars, validation of orphan sidecars, rename tooling that moves both files, and the studio presenting both as one model.
