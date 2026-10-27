# 0020 — Renameable identity, Apache-2.0, synthetic data only

**Status:** Accepted (2026-10-26)

## Context
The Owner chose the name "Sextant" (an instrument that fixes an exact position against known references) but wants to be able to rename the product quickly. The repository is public.

## Decision
- The product name exists only in `identity.toml`; a rename script and a CI check enforce it; previous Ossie vendor names remain accepted as aliases.
- License: Apache-2.0 (patent grant). The imported corpus keeps its attribution in `NOTICE`.
- No real company data, credentials or personal data in the repository. A synthetic data generator reproduces the shape of the Owner's domain (consumers, meter readings, billing, collections, including edge cases such as voided invoices and negative balances). Real data is used only on the Owner's machine for validation.
- A formal trademark search is performed before the first public release.

## Alternatives considered
- MIT: no explicit patent grant.
- Private repository: possible, but the Owner chose public.

## Consequences
Phase 0 delivers the rename script, the product-name check and the synthetic data generator.
