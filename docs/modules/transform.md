# Module pack — `transform`

**Responsibility:** connect to sources and turn them into tested tables in DuckLake; schedule runs from declared freshness.
**Distribution / import:** `orvex-transform` / `orvex.transform`.

## Contracts
- **Owns:** `contracts/model-config/v1`, `contracts/run-record/v1`, OpenLineage emission.
- **Consumes:** `contracts/store/v1`, `contracts/project/v1` (connections), `contracts/preaggregation/v1`, `audit-event` (emits events).

## Required reading
| Document | Sections |
|---|---|
| `architecture/transformation.md` | all |
| `architecture/overview.md` | §2, §3, §6 (project layout) |
| `architecture/traceability.md` | §2 (lineage), §3 (run records) |
| `process/engineering.md` | all |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0006, 0007, 0008, 0015, 0022, 0023, 0025, 0026, 0027.

## Laws that are acceptance criteria
4 (deterministic models, visible freshness), 8 (models as code), 9 (run observability), 11 (exact types), A6 (no silent failures), A10 (freshness).

## Conformance
Model-config contract tests; integration tests against real DuckDB, DuckLake, SQLite, PostgreSQL and ClickHouse; incremental correctness tests (late data with lookback, deletions with full refresh, schema change policies, rollback on failed test).

## Error codes
`architecture/transformation.md` §7.
