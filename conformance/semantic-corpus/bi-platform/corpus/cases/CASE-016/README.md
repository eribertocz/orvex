# CASE-016 — Ambiguous relationship path rejection

Status: **Normative corpus case v0.1**  
Category: **compile-time rejection**

## Purpose

Reject a dimension reference when two semantically different relationship roles are equally valid.

## Source tables

- `orders` — grain: **one row per order**; key: `order_id`
- `customer` — grain: **one row per customer**; key: `customer_id`

## Semantic relationships

- `orders.billing_customer_id -> customer.customer_id (role = billing_customer)`
- `orders.shipping_customer_id -> customer.customer_id (role = shipping_customer)`

## Metric semantics

- `revenue = SUM(amount)`

## Requested semantic query

> Revenue by customer country, without selecting billing or shipping customer role.

## Required planner behavior

Do not guess. Compilation must fail before SQL generation.

## Expected outcome

`expected/error.json` with code `AMBIGUOUS_RELATIONSHIP_PATH`.

## Failure mode this case prevents

Silently choosing one role and returning plausible but semantically wrong numbers.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
