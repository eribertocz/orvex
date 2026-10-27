# Module pack — `mcp`

**Responsibility:** expose the semantic model to AI agents through the Model Context Protocol, acting as the authenticated user.
**Distribution / import:** `sextant-mcp` / `sextant.mcp`.

## Contracts
- **Owns:** `contracts/mcp-tools/v1` (`describe_model`, `query`, `explain`, `list_dashboards`).
- **Transports:** stdio (local) and Streamable HTTP with personal access tokens (server), `security.md` §5.
- **Consumes:** `catalog`, `semantic-query`, `semantic-result`, `receipt`, `security-context`, `dashboard` (page lists supplied by `server`, already filtered), `audit-event` (emits events).

## Required reading
| Document | Sections |
|---|---|
| `architecture/security.md` | §5 |
| `architecture/semantic-engine.md` | §1, §8 (S6), §11 |
| `architecture/traceability.md` | §1 |
| `process/engineering.md` | all |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0016, 0022.

## Laws that are acceptance criteria
1, 2, 3 (AI never bypasses the engine; same permissions), anti-pattern A11.

## Conformance
MCP cases in the security suite; contract tests against the latest stable MCP specification.
