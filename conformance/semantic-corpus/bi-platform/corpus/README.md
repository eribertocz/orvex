# Semantic Problem Corpus v0.1

Status: **Normative semantic corpus v0.1**  
Date: **2026-09-27**  
Cases: **21**

## Purpose

This corpus defines what Semantic Execution Fabric must understand correctly before implementation is allowed to shape the semantics.

It is deliberately independent of Apache Ossie, CSMIR, SQIR, SEIR, SQL dialects and execution engines.

## Rules

1. Source data is deterministic CSV.
2. A valid case has an exact `expected/result.csv`.
3. A rejected case has an `expected/error.json` containing a semantic error category.
4. Case READMEs define semantic intent and required planner behavior, not implementation syntax.
5. SQL text is not part of the corpus contract.
6. An implementation passes a case only if both semantic behavior and result/error match.
7. Later optimizations must preserve every passing result.

## Cases

| ID | Case | Category | Expected |
|---|---|---|---|
| CASE-001 | Additive metric | metric algebra | valid result |
| CASE-002 | One fact plus dimensions | join correctness | valid result |
| CASE-003 | Declared many-to-one join | join cardinality | valid result |
| CASE-004 | Fanout trap across one-to-many detail | fanout reasoning | valid result |
| CASE-005 | Multi-hop dimensional path | join path planning | valid result |
| CASE-006 | Ratio recomputation | metric algebra | valid result |
| CASE-007 | Semi-additive period-end balance | time semantics | valid result |
| CASE-008 | Distinct-count non-additivity | metric algebra | valid result |
| CASE-009 | Multi-fact at the same grain | multi-fact planning | valid result |
| CASE-010 | Multi-fact with different grains | multi-fact planning | valid result |
| CASE-011 | Many-to-many bridge with allocation | many-to-many semantics | valid result |
| CASE-012 | Time grain conversion | time semantics | valid result |
| CASE-013 | Slowly changing dimension as-of join | temporal relationship | valid result |
| CASE-014 | Null semantics | expression semantics | valid result |
| CASE-015 | Mandatory row policy | policy correctness | valid result |
| CASE-016 | Ambiguous relationship path rejection | compile-time rejection | compile error |
| CASE-017 | Incompatible units rejection | semantic type system | compile error |
| CASE-018 | Materialization semantic equivalence | optimizer correctness | valid result |
| CASE-019 | Cross-engine decimal and timezone edge | cross-engine conformance | valid result |
| CASE-020 | Illegal temporal reaggregation rejection | compile-time rejection | compile error |
| CASE-021 | Cumulative metric | window/time metric | valid result |

## Corpus exit gate

normative corpus is complete only when an independent architecture review agrees that these cases cover the minimum correctness frontier for v0.1 and that no implementation-specific construct has leaked into the corpus.

The corpus is expected to grow. Existing normative cases may only change through an ADR when their business semantics were demonstrably wrong or incomplete.
