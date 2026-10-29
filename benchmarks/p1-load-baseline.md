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
- **The incremental cycle uses twice the memory of the full load** (about 10 GiB against 5 GiB) on a 15.6 GiB machine. It stays under the lake's memory limit (three quarters of the machine, about 11.7 GiB), but with little room left. The cause is not known yet; #168 investigates it.
- **The lake grows during maintenance** (2.86 to 2.96 GiB). Why was not measured here; #168 checks what maintenance keeps and when the space comes back.

## How to measure again
Start the `load-baseline` workflow from the Actions tab (inputs `size` and `runs`). Its results are uploaded as an artifact and also written as a `load-baseline results` annotation on the job.
