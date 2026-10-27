# CASE-002 — One fact plus dimensions

Status: **Normative corpus case v0.1**  
Category: **join correctness**

## Purpose

Validate a conventional star-schema query with two many-to-one dimension joins.

## Source tables

- `sales` — grain: **one row per order line**; key: `line_id`
- `customers` — grain: **one row per customer**; key: `customer_id`
- `products` — grain: **one row per product**; key: `product_id`

## Semantic relationships

- `sales.customer_id -> customers.customer_id (many-to-one)`
- `sales.product_id -> products.product_id (many-to-one)`

## Metric semantics

- `revenue = SUM(quantity * unit_price)`

## Requested semantic query

> Revenue by customer region and product category.

## Required planner behavior

Join dimensions only through declared many-to-one relationships, then aggregate line revenue at region × category.

## Expected outcome

Exact grouped revenue is stored in `expected/result.csv`.

## Failure mode this case prevents

Wrong dimension binding or join multiplication in an ordinary star schema.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
