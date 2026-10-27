# CASE-009 — Multi-fact at the same grain

Status: **Normative corpus case v0.1**  
Category: **multi-fact planning**

## Purpose

Combine two independent fact sources that are already aligned at the same semantic grain.

## Source tables

- `sales_monthly` — grain: **one row per month × region**; key: `month + region`
- `refunds_monthly` — grain: **one row per month × region**; key: `month + region`

## Semantic relationships

- None.

## Metric semantics

- `gross_revenue = SUM(sales_monthly.amount)`
- `refunds = SUM(refunds_monthly.amount)`
- `net_revenue = gross_revenue - refunds`

## Requested semantic query

> Gross revenue, refunds and net revenue by month and region.

## Required planner behavior

Aggregate each fact independently if needed, align on month × region, then compute net revenue.

## Expected outcome

2026-01 North: gross 300.00, refunds 20.00, net 280.00; South: gross 100.00, refunds 5.00, net 95.00.

## Failure mode this case prevents

Fact-to-fact raw join multiplication or incorrect grain alignment.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
