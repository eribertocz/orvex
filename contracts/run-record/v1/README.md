# `run-record` contract v1

**Producer:** `transform` and `server` (every `orvex run`). **Consumers:** `server` (`orvex runs`, later the API and UI), lineage and audit (later phases).

One record per model per run; the models of one run share `run_id`.

**When and where they are written (#394).** A successful run inserts its records in **its own transaction**, as its last statements before the commit: the data and the records commit together, so a stop at any point leaves both or neither. A run that fails, is interrupted or cannot commit rolls back, and its records are written **afterwards**, in a transaction of their own, so a failed run is recorded too. Records are written to the lake table `_meta.run_records_v2`, which has every field below except `snapshot_id`: a success record's `snapshot_id` is the snapshot that inserted the record's row (DuckLake's virtual column `snapshot_id`), so it is the snapshot that holds the run's data; any other record's is `null`. The table is only appended to, never deleted from or updated, so maintenance keeps each row's snapshot. `_meta.run_records`, the earlier layout, is read only: nothing writes to it again, and its stored `snapshot_id` stays authoritative for its rows. Both tables exist once any record does; readers read both. A run that changes no data still commits one snapshot, the one holding its records.

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
| `snapshot_id` | The snapshot the run committed, the one holding its records; `null` when it did not |
| `started_at`, `finished_at` | With offset |
| `outcome` | `success`; `failed` (this model failed, or the run rolled back); `not_run` (an earlier model failed first) |
| `error` | `code` and `message` (secrets redacted), or `null` |

Counts are strings so they stay exact (contracts rule). No field holds a secret value.

## Compatibility

New optional fields and new enum values are compatible; renaming or removing a field is breaking (`v2`).
