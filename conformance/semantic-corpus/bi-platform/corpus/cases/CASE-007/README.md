# CASE-007 — Semi-additive period-end balance

Status: **Normative corpus case v0.1**  
Category: **time semantics**

## Purpose

Validate a metric that is additive across accounts but must use period-end semantics across time.

## Source tables

- `balances` — grain: **one row per account per snapshot_date**; key: `account_id + snapshot_date`
- `accounts` — grain: **one row per account**; key: `account_id`

## Semantic relationships

- `balances.account_id -> accounts.account_id (many-to-one)`

## Metric semantics

- `balance = SUM(balance) across account; LAST_VALUE by snapshot_date across time`

## Requested semantic query

> Month-end total balance by region and month.

## Required planner behavior

For each account and month select the latest snapshot, then sum across accounts within region. Do not sum daily snapshots.

## Expected outcome

North: 2026-01 = 300.00; 2026-02 = 330.00.

## Failure mode this case prevents

Summing snapshot balances across dates.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
