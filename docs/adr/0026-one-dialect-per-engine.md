# 0026 — One SQL dialect per engine; no transpilation

**Status:** Accepted (2026-10-26)

## Context
Orvex parses SQL to detect model dependencies, prove incremental filters and validate models. SQLGlot (used by SQLMesh) parses and transpiles between dialects. Transpilation is attractive for multi-source tools, but it is lossy: functions with the same name differ in semantics (rounding, `NULL` handling, time zones, integer division), which can silently change numbers — law 2. Orvex supports one non-attachable source with its own dialect: ClickHouse.

## Decision
1. **DuckDB SQL is the only dialect Orvex parses**, with DuckDB's own parser (`json_serialize_sql`). The semantic engine generates only DuckDB SQL.
2. **Sources that DuckDB attaches** (Parquet, CSV, DuckDB, SQLite, PostgreSQL) are queried in DuckDB SQL; DuckDB pushes filters and projections down.
3. **Sources with their own dialect** (ClickHouse) are read through **extraction models** whose SQL is written in the source's dialect and executed unchanged on the source, read-only, with server-side parameters; results stream as Arrow into DuckDB (`transformation.md` §3.5). Orvex does not parse that SQL; only `filter: manual` is allowed there.
4. **No SQL transpilation** anywhere. A future source with its own dialect follows rule 3.

## Alternatives considered
- SQLGlot to parse and transpile every dialect: one parser for all sources, but transpilation can change results silently, and DuckDB's parser is the exact authority for DuckDB SQL.
- Pulling whole ClickHouse tables and filtering in DuckDB: simple, but transfers everything on each run.
- A ClickHouse DuckDB community extension: does not meet the dependency policy (ADR-0002).

## Consequences
What runs is exactly what the Builder wrote, in the engine that understands it. Automatic incremental filters exist only for DuckDB SQL models; ClickHouse extraction models declare `{since:…}` by hand.
