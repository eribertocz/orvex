# 0021 — JSON logs and OpenTelemetry

**Status:** Accepted (2026-10-26)

## Context
Law 9 requires measuring usage, latency, cache hit rate and degradation.

## Decision
- Structured JSON logs through the Python standard library `logging`, with a request id on every entry; no secrets, personal data or result values.
- Metrics and traces with OpenTelemetry (CNCF), exported over OTLP when configured; no-op otherwise.
- The Builder-facing UI shows each query's duration and whether it hit the cache or a pre-aggregation.

## Alternatives considered
- Vendor-specific monitoring agents: lock-in.

## Consequences
Every module instruments its contract operations with stable metric names documented in its README.
