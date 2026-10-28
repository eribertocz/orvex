# `maintenance-record` contract v1

**Producer:** `server` (`orvex maintain`, and `orvex schedule` once a day). **Consumers:** `server` (`orvex runs --maintenance`, later the API and UI), audit (later phases).

One record per lake maintenance (decision #70, behavior 10; `contracts/store/v1`, `MaintenanceReport`). Records are written to the lake table `_meta.maintenance_runs` in their own transaction **after** the maintenance, so a failed maintenance is recorded too.

| Field | Meaning |
|---|---|
| `run_id` | UUID of the maintenance |
| `trigger` | `manual` (`orvex maintain`) or `schedule` (`orvex schedule`) |
| `retention` | The project's `retention.snapshots` as written (`<n>d`, default `90d`) |
| `cutoff` | Snapshots committed before this instant were expired: the start of the maintenance minus the retention |
| `versions` | DuckDB and every installed module |
| `report` | What the store did (`snapshots_expired`, `files_merged`, `files_created`, `files_rewritten`, `files_deleted`, `orphaned_files_deleted`, and the oldest snapshot kept: `oldest_snapshot_id`, `oldest_snapshot_at`); `null` when the maintenance failed |
| `started_at`, `finished_at` | With offset |
| `outcome` | `succeeded` or `failed` |
| `error` | `code` and `message`, or `null` |

Every snapshot committed at or after `cutoff` stays exactly readable (law 4); a read pinned to an older one fails with `STORE_SNAPSHOT_EXPIRED`.

## Compatibility

New optional fields and new enum values are compatible; renaming or removing a field is breaking (`v2`).
