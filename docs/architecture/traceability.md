# Traceability, reproducibility and caching

## 1. The receipt

Every result returned by the semantic engine carries a receipt (`contracts/receipt/v1`):

| Field | Content |
|---|---|
| `query` | The canonical semantic query (full, not only a hash) |
| `model` | Semantic model fingerprint and project commit |
| `plan` | Plan fingerprint and the discharged correctness obligations |
| `security` | Permission fingerprint; policies applied, described without protected values |
| `evidence` | Evidence used and its status (`declared` or `verified`, with the verification run) |
| `data` | DuckLake snapshot id(s) read, or live source and read instant |
| `freshness` | Last successful run per upstream model and declared freshness |
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

Every dashboard and visual shows "Data as of <timestamp>"; it turns amber when the declared freshness is exceeded and red when the last run failed (the last good data remains visible).

## 6. Caching

See `semantic-engine.md` §10. The cache never crosses permission fingerprints and is invalidated by run records.
