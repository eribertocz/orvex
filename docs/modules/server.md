# Module pack — `server`

**Responsibility:** composition root; HTTP API (FastAPI); CLI (`sextant`); serving the web build; selecting adapters for local or server mode; scheduling workers.
**Distribution / import:** `sextant` / `sextant.server`.

## Contracts
- **Owns:** `contracts/project/v1` (`project.yaml` and `connections.yaml`; other modules add their sections by extending it), `contracts/http-api` (OpenAPI 3.1), CLI interface, instance configuration, project registry, the **project writer** (ADR-0024). Also `contracts/lineage-graph/v1` (composed from the transform graph, the semantic model and dashboard references) and the routing of `preaggregation` definitions from `semantic` to `transform`.
- **Consumes:** every module's contract (the only module allowed to).

## Required reading
| Document | Sections |
|---|---|
| `architecture/overview.md` | all |
| `architecture/security.md` | §2, §3 |
| `process/engineering.md` | all |
| `process/versioning.md` | §3 (contract compatibility at startup) |
| `product/laws.md` | §1–§2 |

## ADRs
0003, 0004, 0005, 0007, 0019, 0020, 0021, 0022, 0024.

## Laws that are acceptance criteria
3, 5, 7, 9, anti-patterns A1, A3, A7, A8.

## Conformance
HTTP contract tests generated from OpenAPI; end-to-end tests in both runtime modes; startup refusal on incompatible contract versions.
