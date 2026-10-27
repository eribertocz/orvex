# 0007 — Projects as files in Git; module state separate

**Status:** Accepted (2026-10-26)

## Context
Modern BI tools (Power BI PBIR, Tableau TWB, Rill) converged on text-based definitions that reference the semantic model and never contain data. Git versioning is required by law 8.

## Decision
- A project is a folder and a Git repository containing connections, models, the semantic model, `security.yaml`, dashboards and a generated `AGENTS.md` (layout in `architecture/overview.md` §6).
- Operational state (users, sessions, memberships, audit log, run history, watermarks, caches) is not part of the project; each module stores its own.
- Secrets are environment-variable references, never values in files.

## Alternatives considered
- Definitions in a database as the source of truth: not reviewable by pull request, violates anti-pattern A5.

## Consequences
Projects can be reviewed, diffed, branched and restored with Git. UI writes go through a single project writer (ADR-0024).
