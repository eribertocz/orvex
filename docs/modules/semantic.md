# Module pack — `semantic` (the core)

**Responsibility:** turn business meaning into correct results — or stable errors — with receipts; single enforcement point for data access.
**Distribution / import:** `orvex-semantic` / `orvex.semantic`.

## Contracts
- **Owns:** `ossie-extensions`, `catalog`, `semantic-query`, `semantic-result`, `receipt`, `semantic-error`, `security-policies`, `matrix`, `preaggregation`.
- **Consumes:** `security-context`, `store`, `run-record`, `audit-event` (emits events).

## Required reading (by phase)

| Phase | Document | Sections |
|---|---|---|
| All | `architecture/semantic-engine.md` | §1, §3, §4, §12, §13 |
| P2 parity | `architecture/semantic-engine.md` | §5, §6, §7, §8 ("Query budgets and cost estimation"), §14 |
| P2 parity | `conformance/semantic-corpus/README.md` | all |
| P3 security | `architecture/semantic-engine.md` | §7, §10 |
| P3 security | `architecture/security.md` | §3.3, §6 |
| P4 S1/S3/S6 | `architecture/semantic-engine.md` | §8 (S1, S3, S6), §10 |
| P5 S2/S4/S5 | `architecture/semantic-engine.md` §8 (S2, S4, S5); `architecture/transformation.md` §8; ADR-0024 (S4 writes) | — |
| P7 matrix | `architecture/semantic-engine.md` §9; `architecture/frontend.md` §4.2 | — |
| All | `architecture/traceability.md` | §1, §4 |
| All | `process/engineering.md` | all |
| All | `product/laws.md` | §1–§2 |

## ADRs
0004, 0009, 0010, 0011, 0012, 0015, 0022, 0025.

## Laws that are acceptance criteria
1, 2, 3, 4, 5, 7, 11, 12 and anti-patterns A1, A2, A6, A11, A12. Every pull request in this module is risk tier A: an independent Reviewer pass reads the full diff (`process/review.md`).

## Conformance
- Semantic corpus (`conformance/semantic-corpus/`): all 21 imported cases plus `CASE-101+`.
- Security suite (`conformance/security/`) for anything touching policies, cache, possible values or matrix.
- Property-based tests for the binder, correctness engine, planner and policy placement.

## Error codes
`architecture/semantic-engine.md` §12.
