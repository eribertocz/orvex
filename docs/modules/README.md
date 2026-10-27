# Module context packs

**Purpose:** an agent working on a module reads **only** that module's pack, the issue it was assigned, and what the pack and the issue link. Nobody reads the whole documentation to implement one issue.

Each pack lists: purpose, contracts it owns and consumes, **required reading** (exact documents and sections), ADRs, laws that are acceptance criteria, conformance suites, error codes, and the roadmap phases where the module is built.

| Module | Pack | Built in phases |
|---|---|---|
| store | [`store.md`](store.md) | P1 |
| transform | [`transform.md`](transform.md) | P1, P5 (pre-aggregation refresh) |
| semantic | [`semantic.md`](semantic.md) | P2, P4, P5 (and support in P3, P7) |
| access | [`access.md`](access.md) | P2 (`security-context`), P3, P9 (personal access tokens) |
| dashboards | [`dashboards.md`](dashboards.md) | P6 |
| mcp | [`mcp.md`](mcp.md) | P9 |
| server | [`server.md`](server.md) | P0, and composition in every phase |
| web | [`web.md`](web.md) | P0 (workspace), P6, P7, P8, P10 |
| contracts | [`contracts.md`](contracts.md) | P0, and every issue that creates or extends a contract |
| conformance | [`conformance.md`](conformance.md) | P0, P3, and the normative-case issues of P4, P5, P7 |
| repo | [`repo.md`](repo.md) | P0, P9, P10 |

## Reading rule (binding, see `AGENTS.md` §2)

1. The issue body.
2. The module pack of the issue's `module:*` label.
3. Only the sections listed under "Required reading" in the pack and in the issue.
4. Anything else only if the issue or a reviewer links it explicitly.

Every agent always applies [`../product/laws.md`](../product/laws.md) §1–§2 (laws and anti-patterns) and [`../process/engineering.md`](../process/engineering.md); they are short and apply to all work, so they are part of every pack's required reading.
