# 0029 — Engine-independent semantic IR, function registry, evidence guards and versioned receipts

**Status:** Proposed (2026-10-31). The Owner approved the direction on 2026-10-31; this record takes effect when the Owner merges it.

**Relation to earlier ADRs.** ADR-0001 makes accepted ADRs immutable; nothing here edits them.
- **ADR-0026 stays in force.** DuckDB SQL is the only dialect parsed, and there is no transpilation. This record restates why SQLGlot is not used as a parser or tree library either (Alternatives).
- **ADR-0009 stays in force.** Ossie is the native format, and only the Ossie adapter knows it.
- **ADR-0028 is unchanged.** Its ClickHouse adapter generates SQL from the IR plan defined here.

## Context
`semantic-engine.md` §4 makes the dialect adapter the only stage that knows a dialect. It does not define how metric and field expressions are represented. Storing DuckDB's parse tree (`json_serialize_sql`, not a stable interface) would couple every stage to DuckDB, and would block a second engine.

Three further gaps:
- §6 calls `declared` evidence "usable", without a check.
- Fingerprints and receipts have no versioning rule.
- Security relies on plan placement alone.

## Decision

### 1. Engine knowledge (enforced by the module-boundary check)

| Component | DuckDB | ClickHouse | Ossie |
|---|---|---|---|
| Ossie input adapter: parse, bind types, convert to IR | yes | no | yes |
| IR, type and stage rules, function registry core | no | no | no |
| Context algebra (S1/S8), correctness core (C1–C9), planner, rewrite catalog, plan validator | no | no | no |
| Receipts and fingerprints | no | no | no |
| DuckDB dialect adapter and its capability table | yes | no | no |
| ClickHouse dialect adapter and its capability table | no | yes | no |
| `store` execution: connections, sandbox, Arrow | yes | yes | no |
| Physical-SQL checks (§7) | yes, for DuckDB SQL | yes, for ClickHouse SQL | no |

DuckDB's parse tree is converted to the IR once. A golden suite of parse trees fails on any format change.

### 2. IR

**Expressions.** Nodes:
- `Column`
- `Literal` — always a bound parameter
- `Attribute` — a security context value, always bound
- `Call(function_id@version, args)`
- `Case`, `Cast`, `Compare`
- `And` / `Or` / `Not`
- `IsNull`, `In`
- `Aggregate(kind, arg, filter, class)`
- `MetricRef`
- `WindowCall(function_id@version, arg, spec)`

All nodes are `dataclass(frozen=True, slots=True)`, with tuple and frozenset fields only.

**Semantic type** = physical type + unit + nullability + exactness (`exact` | `approximate`) + **level**.
- Physical types: `Boolean`, `Integer(bits, signed)`, `Decimal(p ≤ 38, s)`, `Text` (UTF-8, binary order), `Date`, `Timestamp(local | instant)`, `Interval`, `Double`.
- Any `Double` operand makes the result `approximate`.

**Level.** Each expression node is typed by what it is computed over:
- `row(dataset)`
- `aggregated(grain)`
- `windowed(grain, spec)`

Expressions form a **typed dependency graph**, not a linear sequence of stages. The planner orders operators by a topological sort of that graph. The constraints are:
- **L1.** `Aggregate` takes `row` inputs, or `aggregated(g')` inputs where `g'` is strictly finer than the target grain. That is an explicit nested aggregation, for example the average of monthly totals per year; the inner aggregate must be distributive or algebraic in the outer class's terms.
- **L2.** `Recompose` combines inputs of the same grain; aligning different grains is C1/C5 work.
- **L3.** `WindowCall` takes `aggregated(g)` or `windowed(g, …)` inputs at the query's cell grain; nested windows are allowed.
- **L4.** Each window function declares the class of input it accepts.
  - `running_sum` requires an additive input, so `running_sum(ratio)` (a sum of ratios) is rejected.
  - `running_sum(num) / running_sum(den)` (a ratio of sums) is a **different metric with a different meaning**, not an equivalent substitute. The rejection message names both operations and their meanings, and leaves the choice to the author.
  - Likewise, `sum(ratio)` and `sum(num) / sum(den)` are never interchanged by the engine (C2).
- **L5.** A `windowed` value is holistic. It is never re-aggregated to a coarser grain; it is re-evaluated at that grain or rejected.
- **L6.** Each window function declares whether a terminal value is defined when the order axis collapses. If it is not, the cell carries `WINDOW_UNDEFINED_AT_TOTAL`.

**Plan operators.**

| Operator | Purpose |
|---|---|
| `SecureScan(dataset, policies)` | The only way a policy dataset enters a plan |
| `Filter(predicate, origin)` | Origin is intrinsic, context or user |
| `Join(keys, cardinality, evidence, mode)` | See §4 |
| `Aggregate(grain, measures)` | Aggregation to a grain |
| `Align(conformed_dimensions)` | Multi-fact alignment; also aligns a calendar for densification |
| `Allocate(bridge, weights)` | Many-to-many allocation |
| `Recompose(expression over components)` | Ratios and composite metrics |
| `Window(partition, order, frame, densify)` | Windows |

### 3. Type rules and the function registry

**Closed type rules:**
- **Units:**
  - adding or subtracting requires the same unit;
  - multiplying or dividing composes units;
  - anything else is `INCOMPATIBLE_UNITS`.
- **Decimals:** precision and scale follow Orvex rules (`math_rules_version`). Adapters cast explicitly, and overflow is an error.
- **NULL:**
  - every function declares strict propagation or its own rule;
  - every aggregate declares its result on empty input;
  - `NULL` is never zero.
- **Determinism:** non-deterministic functions are rejected in metrics.
- **No implicit coercion:** every combination not defined by the rules is rejected.

**Registry entry.** Each function is registered as `id@semantics_version`, with:
- its signature and type rule;
- its NULL rule and its empty-input rule;
- whether it can fail on data;
- its frame kind, for window functions (rows or time units, §6);
- its terminal-value property;
- its **reference semantics**: a mathematical definition plus a naive reference implementation used in tests.

Per-engine classes and renderings live in each adapter's capability table (ADR-0030). A change in meaning creates a new version; an entry is never edited or deleted.

### 4. Evidence in planning (D1)

| Evidence on the "one" side of a join | Planner behaviour |
|---|---|
| `verified` on the data version the query reads (DuckLake snapshot) | Direct join, or attribute lookup after aggregation |
| `declared`, or `verified` at an earlier instant on a live engine | The fact is isolated by aggregation (C3), and a **uniqueness guard** on the joined keys is evaluated **in the same statement** as the result. A duplicate makes the statement fail with `EVIDENCE_VIOLATED`; no partial result is returned |
| `violated` | Rejected with `EVIDENCE_VIOLATED` |
| Policy-propagation semi-join | Allowed on every status, because `EXISTS` does not depend on uniqueness |

**Consistency between the guard and the result.** The guard is only valid if it reads the same data state as the result.
- **DuckLake:** the statement runs on one pinned snapshot, so the guard and the result read the same state. This is guaranteed by snapshot isolation.
- **Live engines (ClickHouse):** the guard is allowed only if the engine profile certifies (ADR-0030 §5) that one statement reads one consistent state for every reference to the same table. That covers the shared storage-snapshot setting, table engines, replication and distributed tables. [To verify in conformance; the setting reported for ClickHouse 25.6+ is `enable_shared_storage_snapshot_in_query`.]
- **ClickHouse certification.** It is never inferred from a setting alone, including `enable_shared_storage_snapshot_in_query`. It is granted per scenario, and each scenario is tested under concurrent inserts, merges, mutations and lightweight deletes during the statement:
  - single-node `MergeTree` tables;
  - replicated tables, with reads pinned to one replica;
  - distributed tables.

  A scenario that is not certified is not usable for guarded queries.
- **Fail safe:** where that is not certified, a query that needs the guard is rejected with `EVIDENCE_NOT_VERIFIABLE`, and the Builder is told to schedule verification.
- **No partial results, including streaming.** On DuckLake, the guard runs first as its own statement, on the same pinned snapshot as the result statement. No result row is read until the guard has passed. On live engines, the guard is in the result statement, and a guarded plan is **fully buffered by `store`** before any batch is released. A failure discards the buffer, so no row, batch, count or total of a failed statement reaches the caller, a cache or an export.

### 5. Context modifiers (D5)
- A modifier is **statically ineffective** when its lack of effect follows from the metric definition alone, for every possible query context. Example: `keep(d)` inside a metric whose own definition applies `all(d)` innermost. Such a modifier is an **error at publish time** (`CONTEXT_MODIFIER_INEFFECTIVE`), decided by analysis of the definition only.
- When a modifier's effect depends on the query context, for example `remove(d)` while the query does not filter `d`, it is **never an error**. The receipt notes that the modifier had no effect in that query.

### 6. Calendars (D6)
A declared calendar dataset is required **only** when the result depends on periods that may have no rows:
- `densify = true`;
- a window frame in time units, such as "last 3 months";
- a period offset, such as "previous month".

Frames counted in rows, rankings, and running totals over existing rows need no calendar. A time-offset operation without a calendar is rejected, because counting rows instead of periods would be silently wrong.

### 7. Security across transformations (layered; no single layer is claimed sufficient)
1. **Plan:**
   - `SecureScan` is the only entry for a policy dataset.
   - Propagation joins are immovable.
   - Rewrites come from a closed catalog. Each rule states preconditions and proves that the visible-row set of every `SecureScan` is unchanged.
2. **Plan validator.** Independent code rejects a plan with a bare read of a policy dataset, or with aggregation below a policy filter.
3. **Physical SQL.** The generated SQL is checked again: each policy dataset's table reference must sit under its predicate. The DuckDB check uses DuckDB's parser; the ClickHouse check is defined in its adapter. The `store` sandbox applies to all SQL.
4. **Engine side.** Where the engine enforces its own row policies, as in ClickHouse (ADR-0028), they receive the same values.
5. **Behaviour tests on real engines** (criterion SEC-4). Metamorphic security tests run on DuckDB and ClickHouse with optimizer variations, such as join-order and filter-pushdown settings. They cover:
   - values, counts and possible values;
   - totals and caches;
   - errors raised by hidden rows.
6. **Errors (D4).** **No error, in any category, reveals information the caller is not authorized to see.** That includes the names of models, datasets, tables, columns, members, policies and expressions, physical SQL and values.
   - A hidden member gives `MEMBER_NOT_FOUND`, identical to a missing one.
   - Physical names and SQL never appear in caller-facing messages. Only the audit log keeps them, with a correlation id.
   - **Validation and model errors** are shown in full, but name only members the caller can see.
   - **Technical errors** (timeout, budget, engine unavailable) are shown in full, without row estimates for datasets that restrict the caller.
   - **Data-dependent execution errors** (a cast, overflow or division that fails on a value) are shown in full only when the caller's **effective permissions** cover every row and column the statement read: no restricting policy and no masked or hidden member involved. Otherwise the caller gets a generic message with a correlation id, and the detail goes to the audit log.

**Residual risks, documented:** timing differences, and differencing across many queries. They are mitigated by rate limits and audit.

### 8. Versions, fingerprints and receipts (point 9)

**Versions:**
- `ir_version`
- `registry_version`
- `math_rules_version`
- `canonicalization` (`rfc8785` + profile id)

**Fingerprint** = SHA-256 of: domain tag ‖ versions ‖ RFC 8785 bytes. Canonicalization never alters strings. Semantic normalization happens before it:
- names are ASCII by pattern;
- text literals are never normalized;
- each decimal has one form per type, and the scale is part of the type.

**Golden fingerprints.** CI fails when a fingerprint changes without a version bump.

**Three distinct receipt guarantees**, each reported separately:

| Guarantee | Meaning | Required conditions |
|---|---|---|
| **Fingerprint match** | The body is consistent with its `receipt_id`. This detects accidental corruption only: anyone can recompute a SHA-256, so it **does not prove the receipt was not altered** | `receipt_id` recomputes from the canonical body under the recorded canonicalization version |
| **Authenticated integrity** | The receipt was not altered since issue by anyone without the instance's key | An HMAC-SHA-256 over the canonical body, under an instance key identified by `key_id`, verifies. Keys rotate; retired keys are kept for verification only |
| **Issuer authenticity** | A third party can verify which Orvex instance issued it | An asymmetric signature (Ed25519) by the instance's published signing key verifies. Optional in the MVP; without it, receipts claim only authenticated integrity, verifiable by the instance |
| **Rule verifiability** | The recorded plan follows from the recorded model and query under the recorded rules | Authenticated integrity holds; the historical registry entries and rules for the recorded versions are retained. Recompiling reproduces the recorded plan fingerprint |
| **Reproducibility** | Re-executing gives the same value | Rule verifiability holds, and: every construct is of the `exact` class; the data version still exists with the same catalog identity (ADR-0027 §5); the security fingerprint is the same; the engine is within a certified profile. Live engines are never reproducible, only explainable |

Each guarantee is reported separately. A receipt that fails one is reported in that state. It is never re-computed silently under new rules.

### 9. SQL generation
- Values are bound parameters.
- Identifiers come only from the validated IR, through one quoting function.
- Template strings (PEP 750, Python 3.14+) are allowed only through an Orvex processor that enforces both rules.
- `f`-strings, `%` and `.format` are forbidden in adapter SQL code; a lint rule enforces this.
- A test asserts that no data literal appears in generated SQL.

## Alternatives considered
- **Storing DuckDB's parse tree in `CompiledModel`:** least code, but it couples every stage to an unstable format.
- **A linear stage lattice:** insufficient for nested windows and multiple aggregation levels. It was replaced by the typed dependency graph with L1–L6.
- **SQLGlot as parser or tree library:**
  - for SQL that DuckDB executes, a second parser can disagree with the engine silently;
  - tree manipulation happens on the small IR that Orvex owns;
  - translation stays rejected (ADR-0026).
- **Pydantic for the IR:** kept for contracts and boundaries. The IR uses frozen slotted dataclasses.
- **`declared` evidence usable unchecked:** rejected, because a duplicated dimension key would inflate results silently.

## Consequences
- DuckDB format changes are contained in one converter.
- A new engine needs an output adapter, a capability table and the ADR-0030 conformance suite.
- Declared evidence costs a guard per query, or is rejected on engines without certified statement consistency.
- `semantic-engine.md` §4, §6 and §7, `security.md` and `engineering.md` are updated to match (Owner-merged).
- The `roadmap.md` P2-04 text changes from "own RFC 8785 implementation" to "RFC 8785 canonicalization, own or the `rfc8785` package, chosen by the P2 experiment".

## Acceptance criteria (binding for the P2 issues)

| ID | Criterion |
|---|---|
| IR-1 | The corpus's expressions → IR → DuckDB SQL give results identical to the original SQL on the same data. Results are compared, not SQL text |
| IR-2 | The boundary check enforces the §1 table |
| IR-3 | A test walks every IR type and rejects any mutable field type |
| IR-4 | Every undefined type combination is rejected, with one test per rule |
| IR-5 | The golden suite of DuckDB parse trees fails on any format change |
| IR-6 | Extension without cascade: the S7 and S9 changes add registry entries, IR nodes or operators, and do not modify the logic of existing operators. Checked in review |
| LV-1 | Each of L1–L6 has accepting and rejecting cases: nested windows, a ratio over windows, a window over a composite, a running sum of a ratio (rejected), the average of monthly totals, and re-aggregating a window (rejected) |
| LV-2 | `sum(ratio)` and `sum(num) / sum(den)` give different results on a fixture built to make them differ. The engine never substitutes one for the other, and the rejection message names both |
| EV-1 | A `declared` relationship with a duplicate key fails the statement with `EVIDENCE_VIOLATED` and returns nothing. On DuckLake, the guard and the result read one snapshot |
| EV-2 | On a live engine without a certified consistent-statement profile, a query needing a guard is rejected with `EVIDENCE_NOT_VERIFIABLE` |
| EV-3 | A test injects a concurrent change between the reads on ClickHouse; a certified profile either returns a consistent result or fails |
| EV-4 | `violated` evidence is rejected. Policy semi-joins work on every status |
| EV-5 | A failing guard releases nothing. Tests cover DuckLake (separate guard statement on the pinned snapshot) and a live engine (buffered result), including a result larger than one batch |
| EV-6 | Each certified ClickHouse scenario has a concurrency test. A configuration matching no certified scenario rejects guarded queries |
| CX-1 | Statically ineffective modifiers fail at publish. Context-dependent ones never fail, and are noted in the receipt |
| CA-1 | `densify`, a time-unit frame or a period offset without a calendar is rejected. A rank, or a rows frame, without a calendar is accepted |
| SEC-1 | Metamorphic test: changing rows invisible to the caller changes nothing the caller sees: values, counts, order, possible values or totals |
| SEC-2 | Adversarial plan cases: filter after aggregation; removal of a propagation join; a pre-aggregate missing the policy column; a hidden-row count |
| SEC-3 | A hand-built bare-read plan is rejected by the validator, and the same read in generated SQL is rejected by the physical check |
| SEC-4 | SEC-1 runs on real DuckDB and ClickHouse with optimizer variations. An error raised by a hidden row reaches a restricted caller only as a generic message |
| ERR-1 | Error visibility follows effective permissions (§7.6), with tests for an unrestricted caller and for a restricted one |
| ERR-2 | For every error code, a test with a restricted caller scans the message and its details for hidden model, dataset, table, column, member, policy and expression names, SQL text and values. None may appear |
| FP-1 | Golden fingerprints: CI fails on a change without a version bump |
| FP-2 | Receipts record all four versions. Each guarantee has a passing case and a failing case; for fingerprint match and authenticated integrity, the failing case is a receipt that was altered and its fingerprint recomputed |
| FP-3 | RFC 8785 experiment: own implementation vs the `rfc8785` package. Compared on the RFC's reference vectors (existence to verify) and on generated documents, including non-BMP keys. The package is used unless it fails a criterion: conformance, license, maintenance, Python 3.14/3.15 |
| SQL-1 | The lint forbids string-built SQL in adapters. Generated SQL contains no data literal |
| PERF-1 | Hypotheses H1 (model compile) and H2 (query planning) are measured with a documented protocol, and each is reported validated or refuted |
