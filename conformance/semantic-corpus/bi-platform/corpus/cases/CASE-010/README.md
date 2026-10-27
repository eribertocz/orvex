# CASE-010 — Multi-fact with different grains

Status: **Normative corpus case v0.1**  
Category: **multi-fact planning**

## Purpose

Align transactional actuals with monthly targets without duplicating the coarser fact.

## Source tables

- `invoices` — grain: **one row per invoice**; key: `invoice_id`
- `targets` — grain: **one row per month × region**; key: `month + region`

## Semantic relationships

- None.

## Metric semantics

- `actual_revenue = SUM(invoices.amount)`
- `target_revenue = SUM(targets.target_amount) at target grain`
- `attainment = actual_revenue / target_revenue`

## Requested semantic query

> Actual revenue, target revenue and attainment by month and region.

## Required planner behavior

Aggregate invoices to month × region first; targets are already at that grain; align results; compute ratio after alignment.

## Expected outcome

North: actual 300.00, target 400.00, attainment 0.75; South: actual 150.00, target 120.00, attainment 1.25.

## Failure mode this case prevents

Joining monthly target to every invoice and multiplying target values.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
