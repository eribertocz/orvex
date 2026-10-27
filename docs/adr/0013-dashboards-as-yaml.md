# 0013 — Dashboards as YAML validated by JSON Schema

**Status:** Accepted (2026-10-26)

## Context
Power BI's PBIR stores each page and visual in separate JSON files, publicly documented and Git-friendly; Tableau uses XML workbooks; Rill uses YAML canvas dashboards editable visually and as code. There is no adopted open standard for dashboards yet (a proposal exists in Apache Ossie's discussions; the Universal Dashboard Specification is at an early stage).

## Decision
- One folder per dashboard: `dashboard.yaml` plus one YAML file per page, validated by `contracts/dashboard/v1` (JSON Schema).
- Visuals reference semantic members by name; never SQL, never data.
- Layout positions in Sextant's own format.
- The UI writes dashboard YAML through the project writer, with a diff preview and comments preserved (ADR-0024).
- If an open dashboard standard matures, an adapter converts to it.

## Alternatives considered
- Database-stored dashboards: not reviewable in Git.
- Adopting an early-stage dashboard specification: risk of churn.

## Consequences
Dashboards are diffable and reviewable; editors validate and autocomplete through the published schema.
