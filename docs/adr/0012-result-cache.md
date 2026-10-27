# 0012 — Result cache keyed by permission fingerprint, invalidated by runs

**Status:** Accepted (2026-10-26)

## Context
Studied Cube's two-level cache (in-memory results with `refresh_key`; pre-aggregations), whose cache is hard to disable and must be isolated per tenant to avoid serving one tenant's data to another. Apache Ossie does not define caching.

## Decision
- Level-1 result cache; key = query fingerprint + model fingerprint + data snapshot + permission fingerprint.
- Invalidation is event-driven: when `transform` commits a run, results depending on the changed datasets are invalidated. A per-dataset `ttl` and `enabled` flag are declared in the `ORVEX` Ossie extension.
- Pre-aggregations are capability S5, used only with an equivalence proof.

## Alternatives considered
- Polling refresh keys (Cube): unnecessary because Orvex knows exactly when data changes.
- No cache: fails the cache-hit performance target.

## Consequences
The receipt reports cache usage; security tests verify no cross-permission reuse.
