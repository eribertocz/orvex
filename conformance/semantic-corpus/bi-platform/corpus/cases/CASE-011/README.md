# CASE-011 — Many-to-many bridge with allocation

Status: **Normative corpus case v0.1**  
Category: **many-to-many semantics**

## Purpose

Require explicit allocation semantics when distributing an additive fact across a many-to-many bridge.

## Source tables

- `orders` — grain: **one row per order**; key: `order_id`
- `order_campaign` — grain: **one row per order × campaign**; key: `order_id + campaign_id`
- `campaign` — grain: **one row per campaign**; key: `campaign_id`

## Semantic relationships

- `order_campaign.order_id -> orders.order_id (many-to-one)`
- `order_campaign.campaign_id -> campaign.campaign_id (many-to-one)`

## Metric semantics

- `allocated_revenue = SUM(order_total * allocation_weight); weights per order must sum to 1`

## Requested semantic query

> Allocated revenue by campaign.

## Required planner behavior

Join through the bridge using declared allocation weights. Validate weight completeness before aggregation.

## Expected outcome

Campaign A = 110.00; Campaign B = 40.00; total allocated = 150.00.

## Failure mode this case prevents

Double-counting full order value on every many-to-many edge.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
