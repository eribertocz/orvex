# CASE-006 — Ratio recomputation

Status: **Normative corpus case v0.1**  
Category: **metric algebra**

## Purpose

Prove that a ratio is recomputed from additive components rather than averaged or summed from lower-grain ratios.

## Source tables

- `orders` — grain: **one row per order**; key: `order_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount)`
- `order_count = COUNT(order_id)`
- `average_order_value = revenue / order_count; derived non-additive metric`

## Requested semantic query

> Average order value for all regions combined.

## Required planner behavior

Aggregate numerator and denominator to the requested grain, then divide. Never average region-level AOV values.

## Expected outcome

revenue = 500.00; order_count = 4; average_order_value = 125.00.

## Failure mode this case prevents

Average-of-averages or sum-of-ratios error.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
