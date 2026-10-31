# 0030 — Cross-engine semantic equivalence

**Status:** Proposed (2026-10-31). The Owner approved the direction on 2026-10-31; this record takes effect when the Owner merges it.

**Relation to earlier documents.** This record refines the enabling rule of `semantic-engine.md` §4 ("results identical to the managed-lake reference"). It supersedes no ADR. It builds on ADR-0029 (IR and registry) and ADR-0028 (ClickHouse by Direct Query).

## Context
Engines differ in:
- numeric precision and overflow;
- `NULL` handling in joins and empty aggregates;
- integer division;
- collation;
- date ranges and time zones.

A finite corpus cannot prove equality for all inputs. Orvex needs a contract that states exactly what is guaranteed, on which inputs, under which engine conditions, and with what evidence.

## Decision

### 1. Two kinds of equivalence
- **Expression equivalence — guaranteed.** On identical input data, an engine computes an IR construct exactly as its reference semantics defines it.
- **Result equivalence across independent databases — not guaranteed by the engine.** Equal numbers require identical data. Receipts report data versions; whether two databases hold the same data is outside the engine's claim.

### 2. Reference semantics and domains
Every registry construct (ADR-0029 §3) has a mathematical definition and a **declared domain** `D(c)`:
- numeric ranges;
- decimal precision and scale;
- date and timestamp ranges;
- text: UTF-8 with binary byte order — any collation is unsupported in the MVP;
- the time zone database and its version;
- empty-input and `NULL` behaviour.

### 3. Classes per construct and engine
Each engine's capability table assigns one class to each construct:
- **Exact.** For every input in `D(c)`, the result equals the reference: value, type, scale and `NULL`-ness.
- **Approximate (declared).** The result is within a stated bound and marked approximate. In the MVP this applies only to `Double` operations.
- **Unsupported.** The plan is rejected before execution with `ENGINE_CONSTRUCT_UNSUPPORTED`.

Input outside `D(c)` is detected by an in-query check or a type check and fails. It never yields a different number.

### 4. Evidence required for the exact class
All three are required:
1. **Correctness note.** A written argument per construct and engine, from the engine's documentation and the certified profile, reviewed in the pull request. **Tests complement the argument; they never replace it.**
2. **Differential tests** in CI against real engines:
   - domain boundary values;
   - generated values (Hypothesis);
   - empty inputs and `NULL`s.

   Compared exactly, or within the bound for the approximate class.
3. **Runtime checks** wherever the argument depends on data: overflow, range, uniqueness guards (ADR-0029 §4).

### 5. Certified engine profile (D2)
A connection is usable only within a **certified profile**. A profile records:
- **the engine version**, read at every connection. DuckDB is pinned exactly. For ClickHouse, each certified version is listed; **a version that is not listed is not assumed compatible**, even above a minimum;
- **the required settings, read back and asserted.** For ClickHouse:
  - `join_use_nulls = 1`;
  - the empty-aggregate behaviour that makes `sum` over no rows `NULL`;
  - the session time zone;
  - the consistent-statement settings ADR-0029 §4 needs.

  Each one is to be verified in conformance;
- **effective capabilities, probed** at certification on that engine: table engines, replication, distributed tables;
- **certified scenarios** for statement consistency (ADR-0029 §4). Each is proven by tests under concurrent writes; no single setting is accepted as proof;
- **the conformance suite result:** every registry construct on that version and those settings.

A connection whose version, settings or probed capabilities differ from a certified profile is refused until the conformance suite has run on it. The suite is the existing differential suite, run against that connection. Certification is recorded with its date and versions.

### 6. Wording
No document, receipt or interface says "fully equivalent". The claim is: *equal to the reference for the listed constructs within their domains, under the certified profile.*

## Alternatives considered
- **Corpus-only enablement** (the current wording of §4): a finite sample, with no per-construct claim, domains or profile.
- **Tolerances by default:** this hides real differences in exact arithmetic. Tolerance is limited to the declared approximate class.
- **A minimum version as compatibility:** settings and behaviour change between versions. Each version is certified instead.

## Consequences
- Some engine-specific ClickHouse functions stay unavailable until they are proven.
- Each new construct costs a reference definition, a domain, one note per engine and tests.
- Engine upgrades require re-certification. Normally this is automatic: the conformance suite runs on the new version.

## Acceptance criteria

| ID | Criterion |
|---|---|
| EQ-1 | Every registry entry has a class for every engine; CI fails on a gap |
| EQ-2 | Differential tests against real DuckDB and ClickHouse: domain boundaries, generated values, empty inputs and `NULL`s, compared exactly (or within the bound for the approximate class) |
| EQ-3 | Every exact construct has a correctness note per engine, reviewed in its pull request |
| EQ-4 | A connection with an uncertified version, a missing or wrong setting, or a different probed capability is refused, with a test for each |
| EQ-5 | Each unsupported construct, and each out-of-domain input, has a test that rejects or fails it |
| EQ-6 | Documentation and receipts use the claim wording of §6. A check in CI looks for "fully equivalent" |
