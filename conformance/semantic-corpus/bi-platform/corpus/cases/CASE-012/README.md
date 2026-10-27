# CASE-012 — Time grain conversion

Status: **Normative corpus case v0.1**  
Category: **time semantics**

## Purpose

Validate deterministic derivation from day-level events to calendar-month grain.

## Source tables

- `events` — grain: **one row per transaction**; key: `event_id`

## Semantic relationships

- None.

## Metric semantics

- `revenue = SUM(amount)`
- `event_time uses Gregorian calendar; query timezone = UTC`

## Requested semantic query

> Revenue by calendar month.

## Required planner behavior

Derive month from event timestamp using declared timezone/calendar semantics, then aggregate.

## Expected outcome

2026-01 = 150.00; 2026-02 = 70.00.

## Failure mode this case prevents

Engine-specific or ambiguous time bucketing.

## Fixture contract

- `input/*.csv` contains deterministic source data only.
- `expected/result.csv` is authoritative for valid cases.
- `expected/error.json` is authoritative for rejected cases.
- Engine SQL is intentionally absent.
- No Ossie, CSMIR, SQIR or SEIR syntax is normative in the corpus.
