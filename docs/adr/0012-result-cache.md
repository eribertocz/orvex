# 0012 — Result cache keyed by permission fingerprint, invalidated by runs

**Status:** Accepted (2026-10-26); amended in part by [ADR-0027](0027-two-data-routes.md) (cache for datasets outside the managed lake), [ADR-0030](0030-ossie-version-evolution.md) §6 (the key is the execution identity) and #444 (validity by snapshot, no run-record messages)

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

## Amended by #444 (Owner decision of 2026-11-13, Q1 = A)
- **Validity comes only from the key.** Each request pins the snapshot it reads, and the key holds exactly that snapshot (with the permission fingerprint built for the request), so an entry from an earlier snapshot is never matched. Run records are not broadcast: there is no `LISTEN`/`NOTIFY`, and no message whose loss could serve a stale result.
- **Placement.** The cache sits after planning: every refusal and policy is decided anew per request, and only the read is skipped. A hit issues its own cursor and a freshly sealed receipt (`cache: hit`).
- **Consequence:** any commit, even to a table a question does not read, makes it miss once. Finer validity across commits would change the execution identity and is left for later, with its own proof.
