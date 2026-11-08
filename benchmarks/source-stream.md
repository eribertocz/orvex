# Source isolation cost (#372, ADR-0031 §6)

Measured with `tools/benchmarks/source_stream.py` on 2026-11-07. Every number below comes from that run; none is estimated.

- **Code:** the #372 branch on top of `493426e` (main), with the script as committed here; the measured code is that of this pull request
- **Command:** `python -m tools.benchmarks.source_stream --rows 10000000 --rows 50000000 --runs 3`
- **Versions:** DuckDB 2.0.0, Python 3.15.0b4
- **Machine:** the development sandbox: 2 CPUs, 7 GiB of memory, Linux 6.18 (x86_64). This is not a CI runner: there is no workflow for this benchmark, so no CI run is recorded.

## Method
- **Data:** one synthetic table in a DuckDB file: `id` BIGINT, `amount` DECIMAL(18,2), `at` TIMESTAMPTZ, `meter` VARCHAR. It is built by the script.
- **Three paths,** each landing the whole table into a DuckDB table (memory limit 4GB, 4 threads, Arrow batches of 65,536 rows):
  - `direct`: the source attached to the reading instance and copied with `CREATE TABLE … AS`. This is the path a lake used before #372, with no isolation.
  - `stream`: a second DuckDB instance in the same process, streaming Arrow batches. This is in-process isolation, which ADR-0031 §6 rejected.
  - `worker`: the product's path, `source_worker.start`, an Arrow IPC stream from a worker process.
- **Correctness:** every run checks the landed row count and the exact decimal sum against the source's. A wrong landing fails the benchmark.
- **Runs:** 3 per path and size, reporting the median.
- **Memory:** the peak resident memory of the reading process. It includes the landed table, which is kept in memory here. A worker's own peak is not reported, because a spawned child's `ru_maxrss` counts the parent's pages from before `exec`.

## Results
| Rows | Path | Runs (s) | Median (s) | Rows/s | Peak RSS, reading process (MiB) |
|---|---|---|---|---|---|
| 10,000,000 | `direct` | 0.324, 0.349, 0.295 | 0.324 | 30,826,740 | 507.5 |
| 10,000,000 | `stream` | 0.655, 0.651, 0.598 | 0.651 | 15,353,339 | 650.3 |
| 10,000,000 | `worker` | 1.181, 1.233, 1.188 | 1.188 | 8,416,359 | 650.3 |
| 50,000,000 | `direct` | 1.456, 1.415, 1.407 | 1.415 | 35,336,713 | 2191.4 |
| 50,000,000 | `stream` | 2.805, 2.86, 2.876 | 2.86 | 17,479,909 | 2332.0 |
| 50,000,000 | `worker` | 4.534, 4.605, 4.681 | 4.605 | 10,857,514 | 2332.0 |

## Reading
- **The worker path is 3.3 to 3.7 times slower than the direct path.** On these sizes it adds 0.6 to 0.9 s per 10 M rows: the process start, the Arrow IPC serialization and the copy through the pipe. It still lands about 8 to 11 million rows per second.
- **Half the cost is the Arrow stream itself:** the in-process stream already takes about twice the direct time.
- **The boundary does not depend on these numbers** (ADR-0031 §6). A slower path is optimized (batch size, buffers), never bypassed.
