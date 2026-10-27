# Module pack — `conformance`

**Responsibility:** the normative suites that define correct behavior independently of any implementation: the semantic corpus and the security scenarios, plus their runners.
**Location:** `conformance/semantic-corpus/`, `conformance/security/scenarios/` (normative, governed paths) and their runners under `conformance/runners/` (not governed). The pinned Apache Ossie schema is vendored in `vendor/apache-ossie/`.

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
- The imported corpus (`conformance/semantic-corpus/bi-platform/corpus/`) is implementation-neutral: prose intent, CSV input and an exact expected result or error; it has no model and no query. bi-platform's per-case Ossie models are imported unchanged beside it (`models/`), and `SOURCE.toml` records the SHA-256 of every imported file.
- A **non-governed adapter** (`conformance/runners/bi_platform_adapter.py`) is the only code that knows bi-platform's format: its CSV convention (no quoting, empty field is `NULL`, values kept as text), its manifest outcomes, its vendor name `bi-platform` (recognized by the adapter; it is not a previous name of this product, so it is not in `previous_vendor_names`), and an explicit mapping table from its error codes to Sextant codes. Its extension payloads are translated to `SEXTANT` payloads once `ossie-extensions` v1 exists (P2-01). Each case's query is written in the `semantic-query` contract when the engine can run it. Until the engine executes queries (P2-12), the runner loads every case and reports it `not-run`.
- Each case has synthetic deterministic input (CSV), the Ossie model, the query, and either an exact expected result (CSV with explicit `NULL` and decimal scale) or an expected error code. No real data.
- Security scenarios are negative by default: each states what must **not** be returned and the expected code or zero-row result.
- Runners report per case (pass, fail, expected-fail with the issue that will fix it) and are deterministic. A case is never edited to make an implementation pass; a wrong case is fixed through its own normative pull request with the reason.
- Pull requests here are risk tier A (Reviewer pass).
