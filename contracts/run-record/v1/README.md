# `run-record` contract v1

**Producer:** `transform` and `server` (every `orvex run`). **Consumers:** `server` (`orvex runs`, later the API and UI), lineage and audit (later phases).

One record per model per run; the models of one run share `run_id`. Records are written to the lake table `_meta.run_records` in their own transaction **after** the run's transaction commits or rolls back, so a failed run is recorded too.

| Field | Meaning |
|---|---|
| `run_id` | UUID of the run |
| `model` | `<folder>.<name>` |
| `trigger` | `manual`, `schedule`, `freshness`, `full_refresh` |
| `materialized` | `view`, `table`, `incremental` |
| `project_commit`, `project_dirty` | Git commit of the project folder and whether `models/`, `project.yaml` or `connections.yaml` have uncommitted changes; both `null` when the folder is not a git repository |
| `versions` | DuckDB and every installed module |
| `since`, `rows_read`, `watermark` | Incremental values (P1-11+); `null` until measured |
| `rows_written` | Exact, as a string of digits; `null` when the run did not commit |
| `tests`, `reconciliation` | Results (P1-15, P1-16); empty until then |
| `snapshot_id` | The snapshot the run committed; `null` when it did not |
| `started_at`, `finished_at` | With offset |
| `outcome` | `success`; `failed` (this model failed, or the run rolled back); `not_run` (an earlier model failed first) |
| `error` | `code` and `message` (secrets redacted), or `null` |

Counts are strings so they stay exact (contracts rule). No field holds a secret value.

## Compatibility

New optional fields and new enum values are compatible; renaming or removing a field is breaking (`v2`).
