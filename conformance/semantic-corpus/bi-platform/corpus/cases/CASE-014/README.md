# CASE-014 — Null semantics

Status: **Normative corpus case v0.1**  
Category: **expression semantics**

## Purpose

Make null handling explicit and portable across engines.

## Source tables

- `measurements` — grain: **one row per measurement**; key: `measurement_id`

## Semantic relationships

- None.

## Metric semantics

- `amount_sum = SUM(amount) using SQL null-elision semantics`
- `amount_count = COUNT(amount)`
- `row_count = COUNT(*)`

## Requested semantic query

> Return amount_sum, amount_count and row_count.

## Required planner behavior

Preserve declared SQL-compatible null semantics exactly; do not coerce NULL amount to zero before COUNT(amount).

## Expected outcome

amount_sum = 150.00; amount_count = 2; row_count = 3.

## Failure mode this case prevents

Inconsistent null coercion between engines.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
