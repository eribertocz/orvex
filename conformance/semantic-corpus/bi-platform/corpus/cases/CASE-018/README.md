# CASE-018 — Materialization semantic equivalence

Status: **Normative corpus case v0.1**  
Category: **optimizer correctness**

## Purpose

Establish that an aggregate materialization may replace base execution only when it is semantically equivalent for the query.

## Source tables

- `sales` — grain: **one row per sale**; key: `sale_id`
- `sales_month_category_rollup` — grain: **one row per month × category**; key: `month + category`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount)`
- `rollup stores exact SUM(amount) grouped by month × category`

## Requested semantic query

> Revenue by month and category.

## Required planner behavior

Base-plan result and rollup-plan result must be identical. Optimizer may select the rollup only after proving grain/metric/filter compatibility.

## Expected outcome

2026-01 A = 150.00; 2026-01 B = 20.00; materialization is eligible.

## Failure mode this case prevents

Using a rollup whose grain or metric semantics do not exactly satisfy the request.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
