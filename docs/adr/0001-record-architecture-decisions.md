# 0001 — Record architecture decisions

**Status:** Accepted (2026-10-26)

## Context
Sextant is specified and implemented by an AI agent that does not keep memory between sessions. Decisions made in conversations are lost unless they live in the repository.

## Decision
Every product or architecture decision is recorded as an ADR in `docs/adr/`, with context, decision, alternatives considered and consequences. ADRs are immutable once accepted; changes require a new ADR that explicitly supersedes the old one. Product and architecture ADRs require Owner approval: `docs/adr/` is a governed path that only the Owner merges (`process/git-workflow.md`).

## Alternatives considered
- Decisions in a single changing document: loses history and rationale.
- Decisions only in issues: hard to find and not ranked in the precedence order.

## Consequences
Agents read ADRs instead of reconstructing intent. Contradictions are resolved through new ADRs, never silently.
