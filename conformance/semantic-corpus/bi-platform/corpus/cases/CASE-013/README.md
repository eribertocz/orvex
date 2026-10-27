# CASE-013 — Slowly changing dimension as-of join

Status: **Normative corpus case v0.1**  
Category: **temporal relationship**

## Purpose

Assign facts to the dimension version valid at the event time, not the current dimension row.

## Source tables

- `sales` — grain: **one row per sale**; key: `sale_id`
- `customer_history` — grain: **one row per customer validity interval**; key: `customer_id + valid_from`

## Semantic relationships

- `sales.customer_id -> customer_history.customer_id with valid_from <= sale_date < valid_to (temporal many-to-one at event time)`

## Metric semantics

- `revenue = SUM(amount)`

## Requested semantic query

> Revenue by historical customer region.

## Required planner behavior

Perform an as-of/interval relationship lookup using sale_date; exactly one historical dimension row must match each sale.

## Expected outcome

North = 100.00; South = 200.00.

## Failure mode this case prevents

Joining all facts to the latest customer region and rewriting history.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
