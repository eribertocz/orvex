# CASE-021 — Cumulative metric

Status: **Normative corpus case v0.1**  
Category: **window/time metric**

## Purpose

Define cumulative semantics separately from ordinary additive aggregation.

## Source tables

- `sales_daily` — grain: **one row per day**; key: `date`

## Semantic relationships

- None.

## Metric semantics

- `daily_revenue = SUM(amount)`
- `cumulative_revenue = running SUM(daily_revenue) ordered by date from partition start`

## Requested semantic query

> Daily revenue and cumulative revenue by date.

## Required planner behavior

Aggregate daily revenue first, then apply an ordered cumulative operator. The cumulative value is not independently additive across dates.

## Expected outcome

2026-01-01: 100.00 / 100.00; 2026-01-02: 50.00 / 150.00; 2026-01-03: 25.00 / 175.00.

## Failure mode this case prevents

Treating cumulative values as base additive facts or applying the window before daily aggregation.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
