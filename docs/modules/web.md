# Module pack — `web`

**Responsibility:** the user interface: shell, navigation, command palette, dashboards, charts, KPIs, filters, the matrix, the explorer, the receipt panel, the studio.
**Location:** `web/` (SvelteKit, static build served by `server`).

## Contracts
- **Owns:** none (pure consumer).
- **Consumes:** `contracts/http-api` only (types generated from OpenAPI).

## Required reading (by phase)
| Phase | Document | Sections |
|---|---|---|
| All | `architecture/frontend.md` | §1, §2 |
| P6 dashboards | `architecture/frontend.md` | §3, §5, §6 |
| P7 matrix | `architecture/frontend.md` | §4; `architecture/semantic-engine.md` §9 |
| P8 studio | `architecture/frontend.md` | §3 (Studio row); `architecture/transformation.md` §3; `architecture/traceability.md` §2 |
| All | `architecture/traceability.md` | §1, §5 |
| All | `process/engineering.md` | all |
| All | `product/laws.md` | §1–§2 |

## ADRs
0013, 0014, 0019, 0022.

## Laws that are acceptance criteria
1 (no business calculation in the browser), 3 (ECharts JSON only), 4 and A10 (freshness), 6 (progressive levels), 11 (exact values stay strings until formatting), 13 (WCAG 2.2 AA), anti-patterns A1, A2, A7, A8.

## Conformance
End-to-end browser tests; automated accessibility checks on every route; keyboard walkthrough in every UI pull request; bundle size measured per route.
