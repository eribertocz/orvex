# CASE-015 — Mandatory row policy

Status: **Normative corpus case v0.1**  
Category: **policy correctness**

## Purpose

Prove that a mandatory tenant policy is attached before execution and cannot be removed by the query caller.

## Source tables

- `sales` — grain: **one row per sale**; key: `sale_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount)`

## Requested semantic query

> Caller context tenant_id = A requests revenue by region.

## Required planner behavior

Attach mandatory predicate `tenant_id = context.tenant_id` before any unsafe exposure; query filters may only further restrict it.

## Expected outcome

North = 100.00; South = 50.00. Tenant B row is invisible.

## Failure mode this case prevents

Cross-tenant leakage or policy application after aggregation.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
