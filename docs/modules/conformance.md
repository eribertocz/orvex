# Module pack — `conformance`

**Responsibility:** the normative suites that define correct behavior independently of any implementation: the semantic corpus and the security scenarios, plus their runners.
**Location:** `conformance/semantic-corpus/`, `conformance/security/scenarios/` (normative, governed paths) and their runners under `conformance/*/runner/` (not governed).

## Contracts
- **Consumes:** `ossie-extensions`, `semantic-query`, `semantic-result`, `semantic-error`, `security-context`, `security-policies`, `matrix`, `http-api` (the runners drive implementations only through contracts, so a Rust engine can run the same suites).

## Required reading
| Issue area | Document | Sections |
|---|---|---|
| All | `architecture/semantic-engine.md` | §5, §12, §13 |
| Corpus import and runner (P0-11) | `architecture/semantic-engine.md` | §3, §4; `NOTICE`; ADR-0009, ADR-0010 |
| Security scenarios and runner (P3-01, P3-13) | `architecture/security.md` | all |
| Capability cases (P4, P5, P7) | `architecture/semantic-engine.md` | §8 (the capability's section), §9 for the matrix |
| All | `product/laws.md` | §1–§2 |

## ADRs
0009, 0010, 0011, 0012, 0015.

## Rules
- **Normative cases come before the code they test** and are merged by the Owner (`owner-merge`). Their issue title starts with "Normative cases".
- Imported corpus cases (`CASE-001`…`CASE-021`) are kept unchanged, with attribution in `NOTICE`; new cases start at `CASE-101`.
- The imported cases keep bi-platform's own format. A **non-governed adapter** in the runner (specified in P0-11) translates them to Sextant contracts: their model format to Ossie with `SEXTANT` extensions, their vendor name through `previous_vendor_names` in `identity.toml`, and their error codes through an explicit mapping table to Sextant codes. Until the engine executes queries (P2-12), the runner loads every case and reports it `not-run`.
- Each case has synthetic deterministic input (CSV), the Ossie model, the query, and either an exact expected result (CSV with explicit `NULL` and decimal scale) or an expected error code. No real data.
- Security scenarios are negative by default: each states what must **not** be returned and the expected code or zero-row result.
- Runners report per case (pass, fail, expected-fail with the issue that will fix it) and are deterministic. A case is never edited to make an implementation pass; a wrong case is fixed through its own normative pull request with the reason.
- Pull requests here are risk tier A (Reviewer pass).
