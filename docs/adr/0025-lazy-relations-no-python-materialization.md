# 0025 — Data stays in DuckDB: lazy relations, no materialization in Python

**Status:** Accepted (2026-10-26); rule 6 applied to row-policy values and rule 1 amended in part by [ADR-0028](0028-clickhouse-by-direct-query-only.md) (ClickHouse datasets stay in ClickHouse; results leave as Arrow)

## Context
The Owner requires that Orvex always work with DuckDB Python relations, which do not materialize. In DuckDB 2.0 a relation is a lazy query: building it runs nothing (DuckDB only binds it to learn columns and types), and **every read reruns the query**; `con.sql(text, params=...)` binds its parameters on every read; `materialize()` runs once and keeps the rows. Copying data into Python objects (lists, dicts, DataFrames) between steps wastes memory, loses exact types, and breaks with 100 M-row tables on a 16 GB machine.

## Decision
1. **Data never passes through Python between steps.** Transformation models, semantic plans, audits, exports and pre-aggregations are composed as DuckDB SQL or lazy relations and executed inside DuckDB. Python code handles plans, SQL text, parameters and metadata — never intermediate rows.
2. **Writes happen inside DuckDB:** `INSERT INTO … SELECT`, `MERGE`, `CREATE TABLE … AS`, relation `insert_into` / `create`, and `COPY … TO` for files and exports. No fetching rows to write them back.
3. **Each final result is read exactly once.** Because a lazy relation reruns on every read, the code that produces a result reads it once, into Arrow (`to_arrow_table` or `to_arrow_reader` with an explicit `batch_size`), and passes that Arrow data on. Calling `str(rel)`, `len`, `count`, `fetchall` or a second read on the same relation in product code is prohibited.
4. **Every DuckLake read pins its snapshot** (`AT (VERSION => n)`), so any rerun sees the same data and results are reproducible (law 4).
5. **Budgets are in the SQL.** Row limits (`LIMIT page_size + 1` to detect truncation), timeouts and memory limits are applied before reading, never by truncating a Python list.
6. **`materialize()` and in-memory Python results** are allowed only where an issue justifies them (for example a small result reused within one request), with a stated row budget.
7. **Contracts stay language-neutral.** The `store` contract exchanges SQL or plans with bound parameters and returns **Arrow** data (table or record-batch stream); `DuckDBPyRelation` is an implementation detail inside the `store` adapter and never crosses a module boundary.
8. **Verification:** the import-boundary checker also fails on `fetchall`, `fetchone`, `fetchmany`, `.df()`, `.pl()` and `to_pandas` in product code outside an explicit allow-list; the store contract tests assert that a 100 M-row query streams without exceeding the memory budget.

## Alternatives considered
- Fetching results into Python lists or DataFrames between steps: simple, but memory-bound and type-lossy.
- `materialize()` by default: avoids reruns but holds every result in memory.
- Passing relations across modules: fast inside one Python process, but it ties every module to DuckDB's Python API and breaks the Rust replacement goal (ADR-0004).

## Consequences
Memory use is bounded by DuckDB's own limits, not by Python. The semantic engine's output is SQL plus parameters plus a snapshot, executed once by `store`. Code reviews check rule 3 (single read) explicitly, because a lazy relation read twice silently runs the query twice.
