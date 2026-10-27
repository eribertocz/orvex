# 0014 — Frontend stack, design system and own matrix component

**Status:** Accepted (2026-10-26)

## Context
The Owner wants a modern interface (references: Vercel, Linear, Figma) that surpasses them, with the matrix as the most advanced analytical component. bi-platform's matrix design proposed AntV S2 (canvas rendering, packages for JavaScript, React and Vue, no Svelte package).

## Decision
- SvelteKit (static) + Svelte 5 + TypeScript + Tailwind CSS v4 (tokens in `@theme`) + Apache ECharts + Monaco + Svelte Flow + Gridstack (isolated in one component).
- Design direction: open-licensed modern typography, information density without noise, command palette and keyboard-first interaction, first-class dark mode, validated chart palette, i18n (Spanish default, English), WCAG 2.2 AA.
- The matrix is an own Svelte component following the WAI-ARIA grid pattern with virtualization on both axes; all calculation happens in the engine. Other renderers may be added as adapters over `contracts/matrix`.
- ECharts advanced options are accepted only as validated, function-free JSON.

## Alternatives considered
- AntV S2: capable, but canvas rendering complicates accessibility and there is no Svelte package.
- CodeMirror: lighter, but mainly maintained by one person (ADR-0002).

## Consequences
The matrix requires significant UI engineering (phase P7) and is fully accessible and consistent with the design system.
