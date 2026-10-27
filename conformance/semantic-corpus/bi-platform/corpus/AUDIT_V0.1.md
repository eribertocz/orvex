# Semantic Problem Corpus v0.1 — Fixture Audit

Date: **2026-09-27**  
Status: **Passed first deterministic fixture audit**

## Checks performed

- 21/21 case directories exist.
- Every case contains a README and at least one input CSV.
- Every valid case contains `expected/result.csv`.
- Every rejection case contains `expected/error.json`.
- Numerical expectations were recomputed independently with Python standard-library `Decimal`, date/time handling and explicit case logic.
- Materialization equivalence fixture was checked against its base data.
- America/La_Paz boundary behavior in CASE-019 was verified using UTC-04:00 for the supplied 2026 timestamps.

## Result

All deterministic numerical fixture audits passed.

## Important limitation

This audit validates the fixtures and expected truth. It does **not** validate any future CSMIR, SQIR, SEIR, planner or engine adapter implementation.
