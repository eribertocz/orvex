# CASE-008 — Distinct-count non-additivity

Status: **Normative corpus case v0.1**  
Category: **metric algebra**

## Purpose

Prove that a distinct-count metric cannot be re-aggregated by summing child groups.

## Source tables

- `activity` — grain: **one row per customer-region activity**; key: `activity_id`

## Semantic relationships

- None.

## Metric semantics

- `active_customers = COUNT(DISTINCT customer_id); non-additive across region`

## Requested semantic query

> Active customers by region and active customers overall.

## Required planner behavior

Compute each requested distinct count from the appropriate source grain. Overall must be recomputed from customer IDs, not sum of region counts.

## Expected outcome

North = 2; South = 2; overall = 3.

## Failure mode this case prevents

Summing distinct counts (2 + 2 = 4) instead of recomputing overall distinct = 3.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
