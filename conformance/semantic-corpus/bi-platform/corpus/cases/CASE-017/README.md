# CASE-017 — Incompatible units rejection

Status: **Normative corpus case v0.1**  
Category: **semantic type system**

## Purpose

Reject arithmetic between incompatible currency units when no conversion semantic is declared.

## Source tables

- `finance` — grain: **one row per record**; key: `record_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue_usd = SUM(amount_usd), unit USD`
- `revenue_bob = SUM(amount_bob), unit BOB`
- `combined = revenue_usd + revenue_bob (invalid without FX conversion)`

## Requested semantic query

> Combined revenue = revenue_usd + revenue_bob.

## Required planner behavior

Type/unit analysis must fail before execution; no implicit currency conversion is permitted.

## Expected outcome

`expected/error.json` with code `INCOMPATIBLE_UNITS`.

## Failure mode this case prevents

Adding numerically compatible but semantically incompatible measures.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
