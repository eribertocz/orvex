# P1 load-performance baseline

Measured by the `load-baseline` workflow (#69, P1-21), started by hand on 2026-10-29. Every number below comes from that run; none is estimated.

- **Run:** https://github.com/eribertocz/orvex/actions/runs/40954631341
- **Commit:** `963a358a98fdb94cc0e2c5a19205fc6f72d45894`
- **Versions:** DuckDB 2.0.0, DuckLake `417a9117f5`, Python 3.15.1
- **Machine:** GitHub-hosted `ubuntu-latest` runner: 4 CPUs, 15.6 GiB of memory (16,760,344,576 bytes), Linux 7.0 (Azure). That matches the reference machine of `vision.md` §6 (4 cores, 16 GB, SSD). The lake's DuckDB memory limit is three quarters of the machine's memory.
- **Dataset:** synthetic `large`, seed 42 (`tools/synthetic`):

| Table | Rows |
|---|---|
| consumers | 1,000,000 |
| meters | 1,030,000 |
| readings | 60,180,000 |
| invoices | 60,000,000 |
| payments | 59,100,000 |

Generating it took about 2 minutes (the workflow's "Generate the dataset" step).

## Project measured

`tools/benchmarks/load_baseline.py` builds it:
- **Staging, tables:** `staging.consumers`, `staging.meters`.
- **Staging, incremental (`append`, `filter: manual`):**
  - `staging.readings`, cut on `read_at`;
  - `staging.invoices`, cut on `issued_at`;
  - `staging.payments`, cut on `paid_at`.
- **Marts, tables rebuilt in every run:**
  - `marts.zone_monthly_energy`: readings × meters × consumers, per zone, tariff and period;
  - `marts.monthly_collections`: invoices × consumers, with a left join on payments summed per invoice.

Each round starts from an empty lake and runs:
1. the full first load (`run`);
2. one more day of readings is appended to the source: 983,024 rows, the readings of the last period's first reading day, one month later;
3. the incremental cycle (`run`): the three incremental models load what is new, and both marts are rebuilt;
4. maintenance (`maintain`).

Each step is the real command in its own process. Its peak memory is that process's maximum resident set. The lake size is the project's lake folder on disk after the step.

## Results (3 rounds, every step succeeded)

| Step | Wall time, median / max | Peak memory, median / max | Lake size after the step |
|---|---|---|---|
| Full first load | 66.6 s / 67.0 s | 4.98 GiB / 4.99 GiB | 2.82 GiB |
| Incremental cycle (one day appended) | 20.7 s / 21.0 s | 9.98 GiB / 10.03 GiB | 2.86 GiB |
| Maintenance | 5.8 s / 5.8 s | 1.38 GiB / 1.40 GiB | 2.96 GiB |

The three rounds agree closely: wall times within 1% (full load), 3% (incremental cycle) and 7% (maintenance) of each other, and peak memory within 2%.

## What this says
- **The full first load fits easily in the transaction's time limit** (1 hour by default, #77): about 67 seconds for about 181 million source rows and two marts. No decision is needed for P1.
- **The incremental cycle uses twice the memory of the full load** (about 10 GiB against 5 GiB) on a 15.6 GiB machine. It stays under the lake's memory limit (three quarters of the machine, about 11.7 GiB), but with little room left. #168 measured where the peak is; see below.
- **The lake grows during maintenance** (2.86 to 2.96 GiB). #168 measured what maintenance keeps; see below.

## Where the memory goes (#168)

The tool now samples each step's resident memory every 0.25 s and prints a line as each run step starts, so a peak is placed at the step it falls in. One more experiment caps the lake's memory limit at 4 GB for the incremental cycle only, through a benchmark seam (`commands._memory_limit` in the step's own process). Nothing a user can set changed.

Runs on the `large` dataset (seed 42), 2 rounds each, same runner type as above:
- **As configured:** https://github.com/eribertocz/orvex/actions/runs/41000924416 (commit `e4e4164664`).
- **Incremental cycle at 4 GB:** https://github.com/eribertocz/orvex/actions/runs/41000926308 (commit `c9e24c48c5`, experiment branch, not merged).
- **Full load at 4 GB**, 1 round: https://github.com/eribertocz/orvex/actions/runs/40999145097.

| Step | Limit | Wall time (s) | Peak (GiB) | Median of the curve (GiB) | The peak falls in |
|---|---|---|---|---|---|
| Full first load | default (11.7 GiB) | 64.4, 65.9 | 4.95, 4.94 | 0.42, 0.43 | `marts.monthly_collections` (`CREATE OR REPLACE TABLE`) |
| Incremental cycle | default | 19.8, 19.8 | 10.04, 9.98 | 3.97, 3.97 | `marts.monthly_collections` |
| Incremental cycle | 4 GB | 27.8, 28.1 | 4.04, 4.05 | 3.24, 3.48 | `marts.monthly_collections` |
| Full first load | 4 GB | 72.7 | 4.07 | 0.48 | the same mart (run without step tracing) |

What it says:
- **Both peaks are in the same mart.** `marts.monthly_collections` joins 60 million invoices with 59 million payments summed per invoice. It is rebuilt from scratch in every run.
  - In the incremental cycle it peaks at about 10 GiB, twice the full load, although it reads the same rows: invoices and payments gain no rows in that cycle.
  - The incremental steps before it (three `append` models) are not where the peak is.
- **The memory is used because it is there, but it is not only cache.**
  - Capped at 4 GB, the incremental cycle succeeds and stays under the cap, but it is about 40% slower (27.8–28.1 s against 19.8 s). That is more than the 25% #168 set as the line between cache and need.
  - Capped at 4 GB, the full load is 12% slower.
  - So DuckDB spends the extra memory on this join's work, and less memory costs time. It does not hold memory it no longer needs.
- **Why the same join needs twice the memory in the incremental cycle is not isolated.** In the full load, its inputs were written earlier in the same transaction. In the incremental cycle, they are read back from the lake's files. The difference lies inside DuckDB's execution of that statement, not in a step the run could drop, so there is no local fix: #185 isolates it.
- **What this means for a machine:** the lake's limit (three quarters of the machine's memory) is what one run may take. On the reference machine, an incremental cycle of this dataset reaches about 10 GiB of 15.6. It completes in 4 GB if the limit is lowered, about 40% slower.

## What maintenance keeps (#168)

After each step, the tool counts the catalog's files (default DuckDB catalog), from the same run as above:

| After | Files the current snapshot uses (MiB) | Files the catalog lists (MiB) | On disk (MiB) | Scheduled for deletion | Snapshots |
|---|---|---|---|---|---|
| Full first load | 11 (2,886) | 11 (2,886) | 2,886 | 0 | 3 |
| Incremental cycle | 12 (2,901) | 16 (2,919) | 2,919 | 0 | 5 |
| Maintenance | 13 (3,004) | 17 (3,022) | 3,022 | 0 | 8 |

- **Nothing on disk is orphaned or waiting for deletion.** The files on disk are exactly those the catalog lists, and none is scheduled for deletion.
- **The 18 MiB the current snapshot does not use belong to older snapshots kept by the retention** (`retention.snapshots`, 90 days by default). That is the marts' previous versions and the files compaction replaced. They come back when those snapshots expire.
- **Maintenance itself adds 103 MiB to the current snapshot** (12 files to 13), while compaction replaces files.
  - Why the rewritten data is larger than what it replaces is not explained by these totals: they are not split by table. #185 looks at it with a per-table count.
  - Within the retention, the lake grows by what maintenance rewrites, and does not shrink until the replaced versions expire.

## How to measure again
Start the `load-baseline` workflow from the Actions tab (inputs `size` and `runs`). Its results are uploaded as an artifact and also written as a `load-baseline results` annotation on the job.
