# 0010 — Semantic engine scope: SEF parity plus S1–S6; normative corpus

**Status:** Accepted (2026-10-26)

## Context
The semantic engine is Orvex's core. SEF (bi-platform) already implements a staged, correctness-first design and executes 17 of the 18 valid corpus cases; its own roadmap lists missing capabilities S1–S6. The Owner decided not to simplify the engine and to surpass SEF.

## Decision
- The engine implements the full correctness core C1–C9 (grain algebra, additivity, fan-out, path disambiguation, multi-fact, temporal semantics, many-to-many allocation, safe rewrites, obligation discharge) and capabilities S1–S6 (filter context and LOD, selection comparison, time intelligence, governed authoring, equivalent acceleration, explanation and comparison) for v1.0.0.
- Evidence (cardinality, uniqueness, temporal non-overlap, bridge weights) is verified against the data after every load; violated evidence makes dependent queries fail with `EVIDENCE_VIOLATED`.
- The 21 cases of bi-platform's "Semantic Problem Corpus v0.1" are imported unchanged as normative conformance; new cases start at `CASE-101`.
- Full specification: `architecture/semantic-engine.md`.

## Alternatives considered
- A simplified engine for an earlier MVP: rejected by the Owner ("quality before speed").
- Reusing SEF through bindings: possible future implementation behind the same contract.

## Consequences
The semantic engine is the largest work item of the roadmap (phases P2, P4, P5). Any replacement implementation must pass the same corpus.
