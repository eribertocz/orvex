# P2 query-performance baseline

Measured by the `query-baseline` workflow (#364, P2-27b), started by hand on `main`. Every number in "Results" comes from that run; none is estimated. Until the first run on `main` is recorded, this file holds the method only.

## Method

- **Machine:** a GitHub-hosted `ubuntu-latest` runner, with 4 CPUs and 16 GB for a public repository. The run records the exact CPU count, memory and system.
- **Data:** the synthetic `large` dataset, seed 42 (`tools/synthetic`): 1,000,000 consumers, 60 months. It is loaded once by the real `run` command into the load baseline's project (`tools/benchmarks/load_baseline.py`), which builds `marts.zone_monthly_energy` and the other models. The run records the rows per table.
- **Model and questions:** `benchmarks/query-baseline/model.ossie.json` and `benchmarks/query-baseline/questions.json`, fixed in the repository:
  - an aggregate of energy and readings per month and tariff;
  - the same aggregate per zone, filtered to one tariff;
  - a first detail page (1,000 rows, by the table's key);
  - its next page;
  - `catalog`;
  - `explain` of the aggregate.
- **Correctness:** every answer is checked against an independent reference.
  - A question with a `reference` must return exactly the rows of that SQL, which DuckDB runs through the store on the same snapshot. Values are compared as `store/v1` JSON text, so a decimal must match exactly.
  - `catalog` must list the model's members, and `explain` must say `planned`.
  - A wrong answer counts as an error, never as a timing.
- **Rounds:** 3 independent rounds. Each starts a new server process (`serve --no-browser`), and each question gets:
  - 5 warm-up requests, not timed;
  - 50 timed requests at concurrency 1;
  - 200 timed requests from 4 concurrent clients.
- **Measures:**
  - client time, from sending a request to its last byte;
  - p50 and p95 by the nearest-rank method;
  - the maximum;
  - throughput, as completed requests per second at concurrency 4;
  - errors, by status and code;
  - the server process's peak resident memory, sampled every 100 ms.
- **Caches:** the product has no result cache. The operating system's file cache is warm after the warm-up.
- **Recorded:** the commit SHA, the CI run, the versions (Python, DuckDB, DuckLake) and the raw timings as a JSON artifact (`query-baseline-large`).

The server serves one project's requests one at a time: each request takes the project lock and opens the lake (#363). So at concurrency 4 the requests queue, and the baseline shows that cost as it is.

## Rerun

From the repository's Actions tab, run **query-baseline** with size `large` and 3 rounds, or:

```
gh workflow run query-baseline.yml -f size=large -f rounds=3
```

Locally, on any machine (its numbers are not comparable with the runner's):

```
python -m tools.synthetic.generate --size large --seed 42 --out data/large
python -m tools.benchmarks.query_baseline --data data/large --work /tmp/query-baseline --out out --rounds 3
```

## Results

Not recorded yet: the first run on `main` follows the merge of #364's pull request.
