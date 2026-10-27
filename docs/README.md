# Documentation index

This page is the entry point to every Sextant document. It defines **what governs what** and the **precedence order** when two documents disagree.

## Precedence order

When two documents contradict each other, the higher item wins. A contradiction is a defect: it is reported in an issue labeled `type:docs` and resolved by the Owner through a new ADR. No document is overridden silently.

| Rank | Document | Approved by |
|---|---|---|
| 1 | Product laws — [`product/laws.md`](product/laws.md) §1 (laws 1, 2, 3, 5 rank above the other laws) | Owner |
| 2 | Anti-patterns — [`product/laws.md`](product/laws.md) §2 | Owner |
| 3 | Security model — [`architecture/security.md`](architecture/security.md) | Owner |
| 4 | Accepted ADRs — [`adr/`](adr/README.md) (a later ADR supersedes an earlier one only if it says so explicitly) | Owner |
| 5 | Design principles — [`product/laws.md`](product/laws.md) §3 | Owner |
| 6 | Architecture, process and module documents — [`architecture/`](architecture/), [`process/`](process/), [`modules/`](modules/README.md), [`roadmap.md`](roadmap.md) | Owner |
| 7 | Contracts in `contracts/` (once created) — they must conform to ranks 1–6 | Agent, Owner for breaking changes |

[`AGENTS.md`](../AGENTS.md) is the operational summary of ranks 1–6 for agents. If `AGENTS.md` and a ranked document disagree, the ranked document wins and `AGENTS.md` is corrected.

## How to read this documentation

**Nobody reads everything to implement one issue.** An agent working on an issue reads: the issue, the context pack of its module in [`modules/`](modules/README.md), and the sections those two list under "Required reading". Every phase in [`roadmap.md`](roadmap.md) also lists its required reading. The full set below is for the Owner, for the Agent when designing a new phase, and for reviews of cross-cutting changes.

## Documents

| Area | Document | Contents |
|---|---|---|
| Product | [`product/vision.md`](product/vision.md) | Problem, users, roles, core flow, scope |
| Product | [`product/laws.md`](product/laws.md) | 13 laws, 12 anti-patterns, 10 design principles |
| Architecture | [`architecture/overview.md`](architecture/overview.md) | Modules, contracts, runtime modes, repository layout |
| Architecture | [`architecture/semantic-engine.md`](architecture/semantic-engine.md) | The core: pipeline, correctness, parity with SEF, capabilities S1–S6, conformance corpus |
| Architecture | [`architecture/transformation.md`](architecture/transformation.md) | Sources, SQL models, incremental loading, freshness, audits, DuckLake |
| Architecture | [`architecture/security.md`](architecture/security.md) | Identity, roles, groups, page/row/column security, audit, AI access |
| Architecture | [`architecture/frontend.md`](architecture/frontend.md) | Components, the matrix, dashboards, design direction |
| Architecture | [`architecture/traceability.md`](architecture/traceability.md) | Receipts, lineage, run records, reproducibility, caching |
| Modules | [`modules/README.md`](modules/README.md) | **Context packs**: what to read for each module and phase |
| Process | [`process/README.md`](process/README.md) | Roles, Git workflow, responsibility matrix, issues, review, versioning, engineering standards |
| Plan | [`roadmap.md`](roadmap.md) | Phases, versions per phase, minimum issue backlog |
| Decisions | [`adr/README.md`](adr/README.md) | Architecture Decision Records |
| Español | [`es/resumen.md`](es/resumen.md) | Resumen para el Owner |

## Glossary

Terms used with a precise meaning across all documents.

| Term | Meaning |
|---|---|
| **Owner** | The product owner (@eribertocz). Sole authority on product decisions, laws, ADR approval and releases. |
| **Agent** | The AI coding agent (Claude Code) that specifies and implements: writes issues, specifications, code and tests; opens and merges pull requests outside governed paths; prepares releases and creates tags (ADR-0018). |
| **Reviewer** | A subagent the Agent launches with clean context to verify a tier A pull request independently (`process/review.md`). |
| **Governed paths** | Files only the Owner merges: all of `docs/` except end-user docs, `AGENTS.md`, `CLAUDE.md`, `identity.toml`, `LICENSE`, `NOTICE`, normative conformance cases and CI workflows (`process/git-workflow.md` §2). |
| **Builder** | Product role: the person who builds sources, models, metrics and security (initially the Owner). |
| **Author** | Product role: a person who builds dashboards from certified metrics without writing SQL. |
| **Viewer** | Product role: a person who only views shared dashboards. |
| **Project** | A folder (a Git repository) containing one analytic domain: connections, models, semantic model, dashboards, policies and its warehouse. |
| **Instance** | One running Sextant installation. It hosts one or more projects and one set of users. |
| **Module** | A unit with a single responsibility, its own contract, its own state and its own version (see `architecture/overview.md`). |
| **Contract** | The language-neutral definition of what a module receives and returns (JSON Schema, OpenAPI, Arrow schema, SQL, YAML schema). |
| **Semantic model** | The Apache Ossie document (plus Sextant extensions) that defines datasets, fields, dimensions, metrics and relationships of a project. |
| **Semantic query** | A request expressed in metrics, dimensions, filters and ordering — never SQL. |
| **Receipt** | The provenance record attached to every returned number (see `architecture/traceability.md`). |
| **Corpus** | The normative set of semantic cases (input CSV + exact expected result or error) every semantic engine implementation must pass. |
| **Phase** | A roadmap stage that ends with a product pre-release tag (see `roadmap.md`). |
| **Issue** | One medium-sized deliverable: one observable capability in one module, implemented by one pull request (`process/issues.md`). |
| **MVP** | The scope defined in `product/vision.md`, released as **v1.0.0 (stable)**. |
