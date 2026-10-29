# Module pack — `store`

**Responsibility:** store and read analytical data in DuckLake through DuckDB; execute queries on Direct Query engines (ADR-0027).
**Distribution / import:** `orvex-store` / `orvex.store`.

## Contracts
- **Owns:** store interface for plans/SQL with bound parameters, write batches, transactions and snapshots (`contracts/store/v1`, created in P1).
- **Consumes:** `contracts/project/v1` (its `retention` section, P1-20).

## Required reading
| Document | Sections |
|---|---|
| `architecture/overview.md` | §2 (modules), §4 (runtime modes), §8 (technology) |
| `architecture/transformation.md` | §4 (write–audit–publish), §6 (warehouse) |
| `architecture/traceability.md` | §1 (receipt fields `data`, `reproducibility`) |
| `process/engineering.md` | all |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0005, 0006, 0022, 0025, 0027.

## Laws that are acceptance criteria
4 (snapshots, determinism), 7 (budgets), 11 (exact types: `DECIMAL`, `BIGINT`, `TIMESTAMPTZ`, `NULL` preserved through Arrow).

## Conformance
Store contract tests; integration tests with real DuckDB and DuckLake on every supported catalog and data-file location (ADR-0027 §2: DuckDB-file, SQLite and PostgreSQL catalogs; local, network and object-storage data files).

## Key rules
- Never concatenate values into SQL; bound parameters only; no `SET VARIABLE` on shared connections.
- DuckDB memory and thread limits configured explicitly; blocking calls run in worker threads.
- Direct Query in v1.0.0 is an existing DuckLake, attached read-only in a DuckDB instance of its own, tables only (ADR-0027 §3, §8); it is optional and never required to start. Quack and ClickHouse Direct Query come after v1.0.0.
- **ADR-0025:** relations are lazy and rerun on every read; build the query as a relation or SQL with `params`, apply budgets in SQL, read it **once** into Arrow (`to_arrow_reader`/`to_arrow_table` with explicit `batch_size`), pin the DuckLake snapshot. `DuckDBPyRelation` never leaves this module; the contract returns Arrow.
