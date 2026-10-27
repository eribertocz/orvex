# CASE-004 — Fanout trap across one-to-many detail

Status: **Normative corpus case v0.1**  
Category: **fanout reasoning**

## Purpose

Ensure a metric defined at order grain is not multiplied when another requested metric comes from order-item grain.

## Source tables

- `orders` — grain: **one row per order**; key: `order_id`
- `order_items` — grain: **one row per order item**; key: `item_id`
- `customers` — grain: **one row per customer**; key: `customer_id`

## Semantic relationships

- `order_items.order_id -> orders.order_id (many-to-one)`
- `orders.customer_id -> customers.customer_id (many-to-one)`

## Metric semantics

- `order_revenue = SUM(orders.order_total) at order grain`
- `item_count = COUNT(order_items.item_id) at item grain`

## Requested semantic query

> Order revenue and item count by customer region.

## Required planner behavior

Compute order revenue without joining through item rows; compute item count from items; align the independently aggregated results at region grain.

## Expected outcome

North: order_revenue = 150.00, item_count = 3; South: order_revenue = 80.00, item_count = 3.

## Failure mode this case prevents

Naive `orders JOIN order_items` multiplies order revenue by item count.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
