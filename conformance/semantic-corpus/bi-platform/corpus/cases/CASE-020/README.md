# CASE-020 — Illegal temporal reaggregation rejection

Status: **Normative corpus case v0.1**  
Category: **compile-time rejection**

## Purpose

Reject an attempt to sum a semi-additive snapshot metric across time when no valid temporal aggregation was requested.

## Source tables

- `balances` — grain: **one row per account × month_end**; key: `account_id + month_end`

## Semantic relationships

- None.

## Metric semantics

- `balance = SUM(balance) across account; non-additive across month_end`

## Requested semantic query

> SUM(balance) across the entire year with no period-end/average temporal semantic.

## Required planner behavior

Compilation must reject the requested reaggregation rather than inventing a temporal rule.

## Expected outcome

`expected/error.json` with code `ILLEGAL_TEMPORAL_REAGGREGATION`.

## Failure mode this case prevents

Producing a meaningless sum of monthly balances.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
