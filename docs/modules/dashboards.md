# Module pack — `dashboards`

**Responsibility:** read, validate and write dashboard and page definitions; decide page access.
**Distribution / import:** `orvex-dashboards` / `orvex.dashboards`.

## Contracts
- **Owns:** `contracts/dashboard/v1`, `contracts/visual-spec/v1`.
- **Consumes:** `security-context` (page access: `dashboards` is the single page enforcer, returning `PAGE_FORBIDDEN`, and builds page-bound queries from published visuals, `security.md` §3.2), `catalog` (to validate visual references), `semantic-query`, `matrix` (builds page-bound queries), `audit-event` (emits events).

## Error codes
`PAGE_FORBIDDEN`, `DASHBOARD_INVALID` (schema or reference errors, with the path of each problem), `UNSUPPORTED_VISUAL_TYPE`, `DRAFT_CONFLICT`. They are part of `contracts/dashboard`; removing a code or changing its meaning is breaking.

## Required reading
| Document | Sections |
|---|---|
| `architecture/frontend.md` | §3, §5, §6 |
| `architecture/security.md` | §3.2 (page access) |
| `architecture/overview.md` | §2, §3, §6 |
| `process/engineering.md` | all |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0007, 0013, 0014, 0022, 0024.

## Laws that are acceptance criteria
1 (no calculations in dashboards), 3 (page access server-side), 6 (progressive levels keep options), anti-patterns A2, A4, A5.

## Conformance
Dashboard contract tests (valid and invalid YAML); page-access cases in the security suite.
