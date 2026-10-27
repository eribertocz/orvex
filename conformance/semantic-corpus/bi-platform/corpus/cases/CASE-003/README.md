# CASE-003 — Declared many-to-one join

Status: **Normative corpus case v0.1**  
Category: **join cardinality**

## Purpose

Prove that a measure on the many side remains additive after joining to a unique dimension.

## Source tables

- `invoices` — grain: **one row per invoice**; key: `invoice_id`
- `customer` — grain: **one row per customer**; key: `customer_id`

## Semantic relationships

- `invoices.customer_id -> customer.customer_id (many-to-one; target key unique)`

## Metric semantics

- `billed_amount = SUM(amount)`

## Requested semantic query

> Billed amount by customer segment.

## Required planner behavior

Use the declared unique target key as a cardinality guarantee; preserve invoice grain until aggregation.

## Expected outcome

Enterprise = 250.00; Residential = 80.00.

## Failure mode this case prevents

Treating a safe many-to-one join as fanout or failing to enforce cardinality metadata.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
