# P2 query-performance baseline

Measured by the `query-baseline` workflow (#364, P2-27b), started by hand on `main` on 2026-11-07. Every number in "Results" comes from that run; none is estimated.

- **Run:** https://github.com/eribertocz/orvex/actions/runs/42398272305
- **Commit:** `d035e35c6cb408cae1b39cebc46e278b6e5e018e`
- **Versions:** product 0.2.0, DuckDB 2.0.0, DuckLake `417a9117f5`, Python 3.15.2
- **Machine:** GitHub-hosted `ubuntu-latest`: 4 CPUs (AMD EPYC 9V45), 15.6 GiB of memory (16,760,340,480 bytes), Linux 7.0 (Azure)

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

### Lake tables on the measured snapshot

| Table | Rows | Bytes |
|---|---|---|
| staging.readings | 60,180,000 | 948,417,713 |
| staging.invoices | 60,000,000 | 890,205,815 |
| staging.payments | 59,100,000 | 1,167,676,765 |
| staging.meters | 1,030,000 | 11,724,584 |
| staging.consumers | 1,000,000 | 7,712,932 |
| marts.zone_monthly_energy | 9,600 | 124,008 |
| marts.monthly_collections | 240 | 5,705 |

Expected rows per answer, each checked against its reference on every request: aggregate 240, aggregate_filtered 40, aggregate_fact_table 180, detail_large_table 1,000, detail_first_page 1,000, detail_next_page 1,000.

### Latency, throughput and errors

Client time in milliseconds, p50 / p95 / maximum, nearest rank. c1 is 50 requests one at a time; c4 is 200 requests from 4 clients.

| Question | Round | c1 p50 / p95 / max | c4 p50 / p95 / max | c4 req/s | Errors |
|---|---|---|---|---|---|
| aggregate | 1 | 275.3 / 285.5 / 285.9 | 1118.7 / 1169.2 / 1291.7 | 3.55 | none |
| aggregate | 2 | 253.2 / 270.2 / 296.1 | 1041.5 / 1099.2 / 1110.6 | 3.82 | none |
| aggregate | 3 | 258.4 / 270.5 / 301.2 | 1045.1 / 1075.2 / 1099.4 | 3.81 | none |
| aggregate_filtered | 1 | 267.9 / 277.8 / 279.4 | 1087.6 / 1118.6 / 1163.6 | 3.67 | none |
| aggregate_filtered | 2 | 268.5 / 280.5 / 280.8 | 1075.9 / 1134.0 / 1211.5 | 3.71 | none |
| aggregate_filtered | 3 | 253.0 / 265.9 / 309.2 | 1024.1 / 1046.4 / 1082.2 | 3.90 | none |
| aggregate_fact_table | 1 | 568.2 / 584.9 / 637.1 | 2154.5 / 2209.7 / 2220.6 | 1.85 | none |
| aggregate_fact_table | 2 | 565.2 / 582.7 / 593.5 | 2186.4 / 2317.0 / 2357.8 | 1.82 | none |
| aggregate_fact_table | 3 | 536.9 / 550.3 / 565.5 | 2291.4 / 2325.9 / 2349.3 | 1.75 | none |
| detail_large_table | 1 | 282.4 / 293.6 / 295.4 | 1128.8 / 1159.6 / 1187.7 | 3.54 | none |
| detail_large_table | 2 | 287.2 / 299.9 / 317.2 | 1156.0 / 1184.8 / 1332.1 | 3.45 | none |
| detail_large_table | 3 | 302.2 / 309.3 / 315.3 | 1225.1 / 1258.6 / 1267.9 | 3.25 | none |
| detail_first_page | 1 | 294.5 / 306.8 / 328.4 | 1163.9 / 1222.7 / 1311.6 | 3.42 | none |
| detail_first_page | 2 | 295.5 / 311.8 / 330.0 | 1209.1 / 1274.8 / 1316.5 | 3.30 | none |
| detail_first_page | 3 | 313.1 / 322.1 / 347.8 | 1255.2 / 1297.3 / 1315.1 | 3.17 | none |
| detail_next_page | 1 | 319.2 / 358.2 / 455.0 | 1280.6 / 1332.0 / 1392.4 | 3.11 | none |
| detail_next_page | 2 | 334.9 / 355.2 / 358.1 | 1291.0 / 1342.0 / 1373.1 | 3.08 | none |
| detail_next_page | 3 | 350.6 / 359.2 / 385.9 | 1304.1 / 1469.4 / 1490.5 | 2.97 | none |
| catalog | 1 | 152.2 / 160.3 / 187.3 | 621.1 / 649.9 / 656.4 | 6.41 | none |
| catalog | 2 | 156.7 / 165.1 / 173.8 | 634.0 / 653.6 / 685.1 | 6.29 | none |
| catalog | 3 | 151.6 / 159.5 / 161.1 | 612.8 / 642.4 / 665.6 | 6.47 | none |
| explain | 1 | 210.3 / 218.5 / 223.2 | 850.2 / 879.2 / 927.7 | 4.68 | none |
| explain | 2 | 216.6 / 229.4 / 263.3 | 873.0 / 900.7 / 922.9 | 4.57 | none |
| explain | 3 | 208.8 / 219.5 / 261.1 | 857.8 / 895.4 / 1042.8 | 4.63 | none |

### Server peak memory

| Round | Peak resident memory (MiB) |
|---|---|
| 1 | 311 |
| 2 | 303 |
| 3 | 304 |

### Reading it

- Every answer of all 3 rounds equaled its reference; there were no errors.
- A request has a fixed cost of about 150 ms: `catalog`, which reads no data, takes that long. It comes from taking the project lock, opening the lake and compiling the model on every request (#363 decided no cache).
- An aggregate over the 60,180,000 readings takes about 0.55 s; questions over the mart and the 1,000,000 consumers take about 0.25–0.35 s.
- At concurrency 4, requests queue behind the project lock: latency is about four times the sequential one, and throughput stays about the same as at concurrency 1.
- The rounds agree within about 10 %.

No threshold is set yet.
