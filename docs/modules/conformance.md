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
0009, 0010, 0011, 0012, 0015, 0027.

## Rules
- **Normative cases come before the code they test** and are merged by the Owner (`owner-merge`). Their issue title starts with "Normative cases".
- Imported corpus cases (`CASE-001`…`CASE-021`) are kept unchanged, with attribution in `NOTICE`; new cases start at `CASE-101`.
- The imported corpus (`conformance/semantic-corpus/bi-platform/corpus/`) is implementation-neutral: prose intent, CSV input and an exact expected result or error; it has no model and no query. bi-platform's per-case Ossie models are imported unchanged beside it (`models/`), and `SOURCE.toml` records the SHA-256 of every imported file.
- A **non-governed adapter** (`conformance/runners/bi_platform_adapter.py`) is the only code that knows bi-platform's format: its CSV convention (no quoting, empty field is `NULL`, values kept as text), its manifest outcomes, its vendor name `bi-platform` (recognized by the adapter; it is not a previous name of this product, so it is not in `previous_vendor_names`), and an explicit mapping table from its error codes to Orvex codes. Its extension payloads are translated to `ORVEX` payloads once `ossie-extensions` v1 exists (P2-01). Each case's query is written in the `semantic-query` contract when the engine can run it. Until the engine executes queries (P2-12), the runner loads every case and reports it `not-run`.
- Each case has synthetic deterministic input (CSV), the Ossie model, the query, and either an exact expected result (CSV with explicit `NULL` and decimal scale) or an expected error code. No real data.
- Security scenarios are negative by default: each states what must **not** be returned and the expected code or zero-row result.
- Runners report per case (pass, fail, expected-fail with the issue that will fix it) and are deterministic. A case is never edited to make an implementation pass; a wrong case is fixed through its own normative pull request with the reason.
- Pull requests here are risk tier A (Reviewer pass).
- **The security runner** (`conformance/runners/security_scenarios.py`, P3-13): one logical record per (case, listed engine), and one execution per engine instance (`managed`, `existing-ducklake/<catalog>`, `clickhouse/<version>`). Statuses: `pass`; `fail` (a wrong answer or control, a leak, a broken run, any refusal but the limitation); `expected-fail` (a `pending` case refused with `UNSUPPORTED_OPERATION` before any row, nothing leaked); `STALE_PENDING`; `not-runnable` (its surface checked absent) and `STALE_NOT_RUNNABLE`; `engine-pending`; `not-applicable` (a change its catalog cannot have); `not-reproduced` (a variant the runner does not make, never a pass); `EXECUTION_MISSING` (a required ClickHouse version not run). Each case's `variants` run as executions of their own. Each execution also says what it shows of the **protection**: `proven` (it passes, or its aggregate counterpart passes for a run refused for detail pages, `security_counterparts.py`, and a query reaches the attack), `not-demonstrated` (with why) or `not-applicable`. The JSON report (`security_scenarios_report` v1) holds no data value: reasons give shapes and counts only. It is the P3 exit's evidence (`git-workflow.md` §6).
- **When normative documents disagree**, the higher-ranked one wins (`docs/README.md`): a case that contradicts an ADR is corrected through its normative pull request, and the ADR is ratified, not rewritten (ADR-0032). #447 corrected `SEC-DQ-006`, `-007` (an existing lake with a view or a macro is served; nothing they name is: ADR-0027 §3, "tables only" and "no lake macros", proven by `test_existing_lake_wiring.test_a_dataset_on_a_view_of_the_lake_is_refused` and the statement guard's tests) and `SEC-DQ-018` (an overlap is `STORE_SOURCE_INVALID`, ADR-0027 §3, "separate locations").
