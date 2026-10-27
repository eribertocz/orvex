# Module pack — `store`

**Responsibility:** store and read analytical data in DuckLake through DuckDB.
**Distribution / import:** `orvex-store` / `orvex.store`.

## Contracts
- **Owns:** store interface for plans/SQL with bound parameters, write batches, transactions and snapshots (`contracts/store/v1`, created in P1).
- **Consumes:** `contracts/project/v1` (its `retention` section, P1-20) and the per-model `retention` override from `contracts/model-config/v1` (decision #70).

## Required reading
| Document | Sections |
|---|---|
| `architecture/overview.md` | §2 (modules), §4 (runtime modes), §8 (technology) |
| `architecture/transformation.md` | §4 (write–audit–publish), §6 (warehouse) |
| `architecture/traceability.md` | §1 (receipt fields `data`, `reproducibility`) |
| `process/engineering.md` | all |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0005, 0006, 0022, 0025.

## Laws that are acceptance criteria
4 (snapshots, determinism), 7 (budgets), 11 (exact types: `DECIMAL`, `BIGINT`, `TIMESTAMPTZ`, `NULL` preserved through Arrow).

## Conformance
Store contract tests; integration tests with real DuckDB and DuckLake (local DuckDB catalog and PostgreSQL catalog).

## Key rules
- Never concatenate values into SQL; bound parameters only; no `SET VARIABLE` on shared connections.
- DuckDB memory and thread limits configured explicitly; blocking calls run in worker threads.
- Quack is an optional adapter only.
- **ADR-0025:** relations are lazy and rerun on every read; build the query as a relation or SQL with `params`, apply budgets in SQL, read it **once** into Arrow (`to_arrow_reader`/`to_arrow_table` with explicit `batch_size`), pin the DuckLake snapshot. `DuckDBPyRelation` never leaves this module; the contract returns Arrow.
