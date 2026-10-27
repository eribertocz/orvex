# CASE-019 — Cross-engine decimal and timezone edge

Status: **Normative corpus case v0.1**  
Category: **cross-engine conformance**

## Purpose

Force exact decimal semantics and timezone-aware date bucketing to be identical across engines.

## Source tables

- `events` — grain: **one row per event**; key: `event_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount DECIMAL(18,4))`
- `business_day is event_time converted to America/La_Paz before date extraction`

## Requested semantic query

> Revenue by business day in America/La_Paz.

## Required planner behavior

Convert timezone before deriving the day; preserve exact decimal arithmetic.

## Expected outcome

2025-12-31 = 0.1000; 2026-01-01 = 0.3000.

## Failure mode this case prevents

UTC/local boundary drift or binary-float result differences such as 0.30000000000000004.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
