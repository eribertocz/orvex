# 0019 — English in the repository; Spanish-default internationalized UI

**Status:** Accepted (2026-10-26)

## Context
Professional reuse of modules, the imported corpus and Git conventions favor English; the first users speak Spanish.

## Decision
- English for code, identifiers, commits, branches, issues, pull requests and repository documents.
- Questions to the Owner include a plain-language Spanish summary; `docs/es/` holds Spanish summaries for the Owner.
- The UI is internationalized from the first screen: Spanish (default) and English, with `es-BO` and `en` formats.

## Alternatives considered
- Spanish identifiers (the Owner's previous convention): limits external adoption and mixes languages with Git.

## Consequences
No hard-coded UI strings; translations are part of the Definition of Done.
