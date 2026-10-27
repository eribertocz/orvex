# 0015 — Receipts, OpenLineage, run records and write–audit–publish

**Status:** Accepted (2026-10-26)

## Context
Laws 2, 4, 9 and 11 require explaining and reproducing every number. Power BI and Tableau provide lineage views, but not at the level of each displayed number down to the source table.

## Decision
- Every result carries a receipt (`architecture/traceability.md`), shown in the UI as "Where does this come from?" on every number, including matrix subtotals.
- `transform` and `semantic` emit OpenLineage events.
- Every model run produces a run record linking code version, parameters and DuckLake snapshot.
- Model runs use write–audit–publish inside one DuckLake transaction: tests and reconciliation run before commit; failures roll back and leave the last good snapshot visible with a warning.
- Exact numerics end to end; certified/draft status on metrics and dashboards; freshness visible on every visual.

## Alternatives considered
- Lineage only at table level: insufficient for law 2.
- Tests after publishing (dbt default): exposes bad data before detection.

## Consequences
Every module involved in data flow must propagate snapshot ids and fingerprints through its contracts.
