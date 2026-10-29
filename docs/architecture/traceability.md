# Traceability, reproducibility and caching

## 1. The receipt

Every result returned by the semantic engine carries a receipt (`contracts/receipt/v1`):

| Field | Content |
|---|---|
| `query` | The canonical semantic query (full, not only a hash) |
| `model` | Semantic model fingerprint and project commit |
| `plan` | Plan fingerprint and the discharged correctness obligations |
| `security` | Permission fingerprint; policies applied, described without protected values |
| `evidence` | Evidence used and its status (`declared` or `verified`, with the verification run, or the verification instant on a live engine after v1.0.0; ADR-0027 §7) |
| `data` | DuckLake snapshot id(s) read (managed lake, existing DuckLake, and the managed-lake tables read by row policies and their registration checks), or live source and read instant |
| `freshness` | Last successful run per upstream model and declared freshness; for Direct Query, the commit time of the snapshot read (existing DuckLake) or the read instant (live engine, after v1.0.0) (ADR-0027 §5) |
| `execution` | Cache hit or miss, pre-aggregation used, duration |
| `reproducibility` | `exact` (all snapshots retained) or `explainable` (snapshot expired or live read) |

In the UI, every number offers **"Where does this come from?"**, which renders the receipt in business language, with SQL available as a secondary detail.

## 2. Lineage

- `transform` emits OpenLineage events (jobs, runs, input and output datasets, column lineage where derivable) for external collectors.
- Inside Orvex, `server` composes the full graph — source → model → semantic dataset → metric → visual — from the transform dependency graph, the semantic model and dashboard references, and serves it as `contracts/lineage-graph/v1` (P8-05).
- The studio shows lineage from source table → models → semantic datasets → metrics → dashboards and visuals, with Svelte Flow.

## 3. Run records

See `transformation.md` §4. Run records link a DuckLake snapshot to the code version and parameters that produced it.

## 4. Reproducibility

Because the engine is deterministic and every input is versioned (project commit, snapshot, permission fingerprint), a receipt with `exact` reproducibility can be replayed and must return the identical result. The log of each query stores its receipt, not result values.

## 5. Freshness visibility

Every dashboard and visual shows "Data as of <timestamp>"; it turns amber when the declared freshness is exceeded and red when the last run failed (the last good data remains visible). For an existing DuckLake the same colors apply to the snapshot served: amber when it is older than the connection's declared freshness, red when the newest snapshot failed verification (the last verified one remains visible). A visual on a live engine (after v1.0.0) shows "live, read at <time>" instead (ADR-0027 §5, §7).

## 6. Caching

See `semantic-engine.md` §10. The cache never crosses permission fingerprints and is invalidated by run records. For datasets outside the managed lake the key also carries the last-change snapshot of every managed-lake table read by their policies or by their registration check, checked on every lookup, so a revoked mapping or registration is never served from cache; live datasets (after v1.0.0) are cached only with a declared `cache.ttl` (ADR-0027 §7).
