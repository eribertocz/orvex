# Frontend (`web`)

## 1. Stack

SvelteKit with the static adapter (served by `server`, no Node.js at runtime), Svelte 5, TypeScript (strict), Tailwind CSS v4 (CSS-first configuration with `@theme` design tokens), Apache ECharts, Monaco, Gridstack (isolated), Svelte Flow, and Orvex's own matrix component. API types are generated from `contracts/http-api` (OpenAPI).

## 2. Design direction

Reference products: Vercel, Linear and Figma — and the goal is to surpass them for analytics.

- **Typography:** an open-licensed (SIL OFL) modern family such as Geist or Inter, with tabular numerals for all numbers.
- **Density without noise:** information-dense layouts, minimal chrome, generous alignment, no decorative elements.
- **Keyboard-first:** command palette (⌘K / Ctrl+K) for every action; shortcuts for navigation, filters and editing; every action reachable without a mouse.
- **Instant feedback:** optimistic UI for local actions, skeletons for loading states, transitions under 150 ms, no layout shift.
- **Dark mode as a first-class theme**, not an afterthought; both themes validated for contrast.
- **Design tokens** (color, spacing, radius, typography, motion) defined once in Tailwind v4 `@theme` and shared by charts and the matrix.
- **Chart palette** validated for color-vision deficiencies and for both themes.
- **Internationalization:** Spanish (default) and English; `es-BO` and `en` number, currency and date formats; no hard-coded strings.
- **Accessibility:** WCAG 2.2 AA (law 13).

## 3. Main components

| Component | Purpose | Key rules |
|---|---|---|
| **Matrix (pivot)** | Multidimensional analysis with hierarchies | See §4 |
| **Detail table** | Unaggregated rows | Server-side sorting and pagination; column security applied by the engine |
| **Charts** | Bar, line, area, combo, scatter, heatmap, funnel, pie/donut, maps (choropleth and points for GIS data) | ECharts; options validated JSON only (law 3) |
| **KPI card** | Value, comparison, variance, sparkline | Comparison computed by the engine (S3) |
| **Filters** | List, search, range, relative dates, hierarchical; scope visual / page / dashboard | Possible values from the engine (S2), authorized rows only |
| **Interactions** | Cross-filtering, drill-down, drill-through | Drill paths declared in the semantic model |
| **Dashboard canvas** | Pages, sidebar, grid layout | Gridstack inside one component; positions stored in Orvex YAML |
| **Explorer** | Ad hoc analysis on the semantic model | No SQL; semantic queries only |
| **Receipt panel** | "Where does this come from?" on every number | Renders the receipt (traceability.md) |
| **Export** | CSV, Excel (keeps matrix structure), PDF | Server-side, through the engine, audited |
| **Studio** | SQL/YAML editing, model runs, lineage, metric authoring | Monaco; Svelte Flow; validation markers from the server |
| **Freshness badge** | Data age per visual and page | Amber when stale, red when last run failed |

## 4. The matrix

The matrix is the most advanced component and the centerpiece of analysis.

### 4.1 Capabilities

- Row and column hierarchies; expand/collapse (+/−) per node and per level; expand all / collapse all.
- **Stepped** layout (all row levels in one indented column) and **tabular** layout (one column per level).
- Subtotals per level, toggleable, positioned at the top or bottom of each group; grand totals for rows and columns.
- Multiple measures, displayed in columns or in rows.
- **Show value as:** % of row total, % of column total, % of grand total, % of parent, difference and % difference to previous period (S1, S3).
- Time-intelligence columns: previous year, variance, year-to-date (S3).
- Conditional formatting: background and font color scales, data bars, icons, rules; declarative only.
- Sparklines in cells.
- Sorting by header or by measure within each level.
- Drill-down and drill-through; cross-filtering to other visuals.
- Frozen headers; resizable columns; virtualization on both axes for thousands of rows and columns.
- Receipt on every cell, including subtotals and totals.
- Export to Excel preserving hierarchy, subtotals and formatting.

### 4.2 Correctness rules (from bi-platform M07 and law 1)

1. Subtotals and totals are computed by the engine (grouping sets), never by summing visible cells.
2. Ratios and non-additive metrics are recomputed at every total level.
3. Pagination and virtualization never change totals.
4. Expanding a node never reveals rows a policy forbids.
5. `NULL` is always distinguishable from zero.
6. Currency formatting never alters decimal precision.
7. Matrices exceeding configured width or cell limits are refused with an explanation (`MATRIX_TOO_WIDE`) and a suggestion; never silently truncated.
8. Each cell is typed data (value, nullity, role `detail`/`subtotal`/`total`, format), never pre-formatted text.

### 4.3 Rendering

- Own Svelte component following the WAI-ARIA **grid** pattern (W3C): full keyboard navigation, screen-reader announcements of headers and values.
- DOM-based with windowed virtualization; the engine returns cell blocks by viewport.
- The component only renders; all calculation happens in the engine.
- Contract: `contracts/matrix/v1` (`MatrixDefinition`, `MatrixQuery`, `MatrixResult`). Alternative renderers (for example AntV S2) may be added later as adapters over the same contract.

## 5. Dashboards

```
dashboards/consumption/
├── dashboard.yaml        # title, semantic model, page order, global filters, theme
└── pages/
    ├── overview.yaml     # title, access groups, layout (grid positions), visuals, page filters
    └── arrears.yaml
```

- Format: YAML validated by `contracts/dashboard/v1` (JSON Schema, published so editors autocomplete).
- Visuals reference metrics and dimensions **by name**; dashboards never contain SQL or data.
- Positions are stored as `{x, y, w, h}` in Orvex's format, never in Gridstack's.
- Narrow screens reflow automatically to a single column; an optional `mobile_order` per page overrides the order. One design serves desktop, tablet and mobile.
- Drafts are visible only to their author; publishing makes them visible to everyone allowed by roles and groups.
- UI writes go through the project writer (ADR-0024): form edits change only the dashboard nodes they own, preserve comments and show a diff preview; code editors (Monaco) save models, the semantic model and `security.yaml` exactly as typed.

## 6. Progressive levels for visuals (law 6)

The visual contract (`contracts/visual-spec/v1`) is shared by three editors: basic (metrics, dimensions, filters), intermediate (axes, series, types, colors, tooltips) and advanced (ECharts options as validated JSON). Switching to a lower level flags options it cannot edit and keeps them intact.
