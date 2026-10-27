# CASE-001 — Additive metric

Status: **Normative corpus case v0.1**  
Category: **metric algebra**

## Purpose

Prove that a distributive additive metric aggregates correctly across a simple partition.

## Source tables

- `sales` — grain: **one row per sale**; key: `sale_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount), additive across region and sale_date`

## Requested semantic query

> Revenue by region.

## Required planner behavior

Aggregate `sales.amount` once at the requested region grain. No join or special rewrite is required.

## Expected outcome

`expected/result.csv`: North = 150.00; South = 200.00.

## Failure mode this case prevents

Incorrect basic aggregation or accidental row multiplication.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
