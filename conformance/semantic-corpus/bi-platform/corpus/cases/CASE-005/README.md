# CASE-005 — Multi-hop dimensional path

Status: **Normative corpus case v0.1**  
Category: **join path planning**

## Purpose

Validate deterministic traversal across more than one many-to-one relationship.

## Source tables

- `sales` — grain: **one row per sale**; key: `sale_id`
- `store` — grain: **one row per store**; key: `store_id`
- `region` — grain: **one row per region**; key: `region_id`

## Semantic relationships

- `sales.store_id -> store.store_id (many-to-one)`
- `store.region_id -> region.region_id (many-to-one)`

## Metric semantics

- `revenue = SUM(amount)`

## Requested semantic query

> Revenue by region name.

## Required planner behavior

Resolve the only valid path sales → store → region, preserve cardinality, then aggregate.

## Expected outcome

Central = 130.00; Altiplano = 70.00.

## Failure mode this case prevents

Incorrect path discovery or loss of cardinality information over multiple hops.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
