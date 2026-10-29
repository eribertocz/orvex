# 0006 — DuckDB as the only analytical engine; DuckLake as the warehouse

**Status:** Accepted (2026-10-26); superseded in part by [ADR-0027](0027-two-data-routes.md) (Quack; the catalog per mode becomes a default); "only analytical engine" to be superseded when ClickHouse Direct Query is built, after v1.0.0

## Context
DuckDB 2.0 LTS is fast, embeddable and standard-SQL friendly. A single DuckDB file allows only one read-write process. DuckLake adds ACID transactions with one snapshot per commit, time travel, and supports a PostgreSQL catalog for multi-user concurrent access. Quack (DuckDB's remote protocol) became a stable extension in DuckDB 2.0 while its documentation states it is still being refined toward full production readiness.

## Decision
- DuckDB is the only analytical engine, always at its latest stable release (ADR-0022).
- DuckLake is the warehouse format from the MVP:
  - local mode: DuckDB-file catalog (recommended by DuckLake for local single-client use);
  - server mode: PostgreSQL catalog (recommended by DuckLake for multi-user use).
- Quack is an optional adapter of `store`, never required to start.
- Sources are read through DuckDB readers and official extensions; ClickHouse through `clickhouse-connect` (ClickHouse, Inc.) returning Arrow.
- DuckDB memory and thread limits are configured explicitly.

## Alternatives considered
- A plain `.duckdb` file only: no snapshots, so receipts could not be exactly reproducible; single-writer limits.
- ClickHouse as the analytical engine: a server dependency contrary to the local-mode principle; it remains a source.

## Consequences
Receipts reference DuckLake snapshots; write–audit–publish uses DuckLake transactions; snapshot retention is configurable and defines exact reproducibility.
