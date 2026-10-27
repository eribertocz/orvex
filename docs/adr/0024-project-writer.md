# 0024 — Project writer: how the UI writes project files

**Status:** Accepted (2026-10-26)

## Context
Projects are files in Git (ADR-0007). The Builder edits SQL models, sidecars, the Ossie model and `security.yaml` in Monaco inside Orvex; Authors build dashboards visually; governed metric authoring (S4) changes the semantic model. Tools that rewrite files on save (reformatting YAML, reordering keys, dropping comments) produce noisy diffs and destroy the Builder's intent — a common complaint about generated BI files. Tools that keep definitions only in a database lose Git review entirely.

## Decision
1. **One writer.** Only the `server` module's **project writer** writes project files. Other modules read files and return validated definitions; they never write.
2. **Code editors save text exactly as typed.** Monaco saves the bytes the user wrote: no reformatting, no key reordering, comments preserved. The writer validates against the contract's JSON Schema and, for SQL models, with the engine that will run them — DuckDB's parser for DuckDB SQL, ClickHouse `EXPLAIN SYNTAX` for ClickHouse extraction models (ADR-0026) — **before** saving; an invalid file is rejected with its errors and nothing is written.
3. **Form edits only where forms exist:** dashboards and pages (layout, visuals) and governed metrics (S4). A form edit changes only the nodes it owns, keeps comments and unrelated text intact, and shows a **diff preview** the user confirms before saving.
4. **Drafts live outside Git.** Unpublished dashboard and metric drafts live in module state (`dashboards` drafts; semantic proposals) until published; publishing writes the files.
5. **Atomic writes.** Each save writes to a temporary file and renames it; a multi-file change (for example a dashboard with pages) is written as one unit or not at all. A file changed on disk since it was opened is not overwritten: the user sees a conflict with both versions.
6. **Git in server mode.** Each publish or save from the UI creates one commit with the acting user's identity as author and a generated message (`<action>: <object>`). In local mode the writer only writes files; committing is left to the Builder, and the UI shows uncommitted changes.
7. **Permissions.** Writes follow roles (security §3.1): Authors write dashboards and metric proposals; only Builders write models, connections, the semantic model and `security.yaml`. Every write is audited.

## Alternatives considered
- Regenerating whole files from an in-memory model on each save: simple, but loses comments and formatting and produces noisy diffs.
- Database as the source of truth with export to files: violates law 8 and anti-pattern A5.
- Letting each module write its own files: several writers, inconsistent atomicity and auditing.

## Consequences
Diffs show exactly what the user changed. Form edits are implemented as **text-span edits**: PyYAML's `compose` gives each node's start and end positions in the file, and the writer replaces only the text of the nodes that changed (or inserts new ones with the file's indentation), then re-parses and verifies that the result equals the intended document. No comment-preserving YAML library is added (the common one has a single maintainer, ADR-0002). `P5-07` implements the writer (first needed to publish governed metrics and, in P6, dashboards); `P8-01` adds the file-editing API used by the Studio. This ADR refines ADR-0007 and ADR-0013 on who writes which files.
