# 0028 — ClickHouse by Direct Query only; extraction retired

**Status:** Accepted (2026-10-30). Owner decision #222 (option C).

This record changes earlier ADRs as follows:
- **ADR-0027** is superseded in part:
  - §2: ClickHouse is no longer a route A source, and extraction is not kept as an option;
  - §9 and §10: ClickHouse Direct Query moves from "after v1.0.0" into v1.0.0 (§3 below).

  Quack stays after v1.0.0.
- **ADR-0026** is superseded in part:
  - rule 1: the semantic engine also generates ClickHouse SQL, from the plan, for ClickHouse datasets;
  - rule 3: there are no extraction models;
  - rule 4, second sentence: a source with its own dialect is queried directly, through a dialect adapter, never transpiled.

  Rule 4's first sentence stands: no SQL transpilation anywhere.
- **ADR-0006:** "DuckDB is the only analytical engine" is superseded. DuckDB remains the managed lake's engine, and ClickHouse is queried in place.
- **ADR-0025:** rule 1 is amended for ClickHouse datasets. Their data stays in ClickHouse, and results leave as Arrow, as from DuckDB.

The rest of these records stands.

## Context
ADR-0027 copied ClickHouse into the managed DuckLake through extraction models until v1.0.0, and kept extraction as an option afterwards. The Owner objected that copying raw data out of one analytical engine into another does not fit DuckLake's purpose. DuckLake holds what a project transforms from raw sources. An analytical engine such as ClickHouse already stores, transforms and serves its own data.

The leaders work the same way:
- Rill queries ClickHouse live, with "no data ingestion or orchestration";
- Cube queries the source warehouse and caches only rollups;
- Looker is live only.

Extraction was also the most complex and error-prone part of P1. It needed `{since:…}` lineage, zone, precision and first-run rules, and most Reviewer rounds of 2026-10-29 and 2026-10-30 were spent on it.

The Owner asked for a product with a single, unambiguous purpose per part, and a minimal one.

## Decision

### 1. One route per kind of data
| Data | Route | Where it is transformed | Receipt |
|---|---|---|---|
| Raw sources: CSV, Parquet, DuckDB, SQLite, PostgreSQL | A — transformed into the managed DuckLake | In Orvex, SQL models (DuckDB SQL) | Exact (snapshot) |
| An existing DuckLake | B — Direct Query, read-only (ADR-0027 §3) | Where it was built | Exact (verified snapshot) |
| ClickHouse | B — Direct Query, read-only | Inside ClickHouse, with its own tools | Explainable (read instant) |

There is exactly one route for each kind of data. Orvex never copies an analytical engine's data into the managed lake. Pre-aggregations are never written to the managed lake for a ClickHouse dataset. The semantic engine accelerates such a dataset only with what ClickHouse itself provides, or not at all.

### 2. Extraction is retired
- The ClickHouse extraction models are removed in P1 (`transform`: the ClickHouse source, `{since:…}` binding and its validation rules), together with their contract text in `contracts/model-config/v1` and `transformation.md` §3.5.
- `clickhouse-connect` stays only if the Direct Query adapter (§3) uses it. Otherwise it is removed.
- Until §3 is built, a project cannot reach ClickHouse. This is acceptable in the alpha phases.

### 3. ClickHouse Direct Query in v1.0.0
The design is ADR-0027 §§4–8 and §10, now built before v1.0.0:
- **SQL:** a ClickHouse SQL dialect adapter generates SQL from the plan, with bound parameters only. Nothing is transpiled.
- **Read-only access and budgets** are enforced on the server: `readonly = 1` and execution limits as settings, verified at connection, adapted to query budgets.
- **Types:** exact. `Decimal`, `Int128`, `UInt64` and their kin never become floats, and `NULL` is never zero.
- **Cost:** estimated with `EXPLAIN ESTIMATE` where the table engine supports it.
- **Security:** policies run inside ClickHouse. Row-policy values come from the managed lake as bound parameters, and `CROSS_ENGINE_QUERY` forbids joins across engines.
- **Receipts and cache:** receipts carry the read instant. Evidence is verified on the connection's schedule (ADR-0027 §§5, 7).
- **Acceptance:** the semantic corpus and the security suite give results identical to the reference on ClickHouse.

It is placed in P2, after the Direct Query foundation (P2-29). Its security cases join P3-13.

## Alternatives considered
- **A — keep ADR-0027 as decided:** already built, but it duplicates data another analytical engine already serves, and keeps the most error-prone part of P1.
- **B — Direct Query in v1.0.0, extraction kept as an option:** two routes for the same source make every Builder choose between them, and the extraction machinery has to be maintained.
- **Polars or ADBC connectors for ClickHouse:** a different transport, but still a copy into the lake.

## Consequences
- **One clear rule for every kind of data:** DuckLake holds what Orvex transforms, and analytical engines are queried where they are.
- **Delay:** v1.0.0 is delayed by the ClickHouse adapter and its security conformance.
- **Receipts:** ClickHouse receipts are explainable, not exact. A join between ClickHouse data and another source is done inside ClickHouse.
- **Closed work:** the open extraction issues are closed as not planned: #164 (PR #205), #190, #210, #218 and #220, and the ClickHouse items of #194.
