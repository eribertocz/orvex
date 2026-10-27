# Phase 1A — CSMIR representation coverage

This matrix describes **model representation and definition-local compilation**, not
full corpus conformance. The 21 normative fixtures and their expected truth remain
unchanged. A corpus case passes the original contract only when its requested
semantic behavior and exact result/error also pass; Phase 1A does not execute those
queries, produce SEIR, discharge obligations, or certify results.

The executable matrix is `crates/sef-model-ir/tests/corpus_models.rs`. Each numbered
test constructs the corresponding CSMIR definitions, checks that every source CSV
and header column has an explicit logical binding, compiles the model, checks
fingerprints for its datasets, fields, keys, dimensions, measures, metrics,
relationships, bridges and policies, and verifies that reordering unordered
collections preserves the model fingerprint. CSV row values and expected outputs
are not evaluated. The tests use logical source handles, not executable CSV adapters.

Every compiled fixture is additionally checked against the frozen artifact hash in
`crates/sef-model-ir/tests/csmir-golden-hashes.txt`. The integrity check in
`scripts/check_phase_1a.py` verifies all 81 original corpus files against the
blueprint checksums without changing fixture truth.

| Fixture | Definition coverage in Phase 1A | Deferred normative behavior |
|---|---|---|
| CASE-001 | Sale grain; region dimension; fully additive revenue | Grouped exact revenue |
| CASE-002 | Fact/dimension schemas; customer/product key domains; two many-to-one roles; explicitly typed quantity × price | Safe star joins and grouped revenue |
| CASE-003 | Invoice/customer grains; declared target uniqueness; `DeclaredUnverified` cardinality accepted | Uniqueness evidence verification and cardinality-preserving execution |
| CASE-004 | Distinct order and item measure grains; customer path | Fanout prevention and independent aggregation/alignment |
| CASE-005 | Sales → store → region relationship graph with explicit roles | Path discovery and multi-hop execution |
| CASE-006 | Revenue/count dependencies; explicit integer-to-decimal cast; ratio; decomposition; zero-denominator and rounding policies | Recompute components at the requested grain and exact division |
| CASE-007 | Account/snapshot composite key; snapshot role; semi-additive period-end axis; account selection scope; uniqueness tie contract | Latest snapshot per account/month followed by account aggregation |
| CASE-008 | Exact distinct-count measure; non-additive classification; region distinct-recompute axis | Recompute the overall distinct set |
| CASE-009 | Two independent facts; explicit conformed month/region dimensions; equal observation grains; typed subtraction | Fact alignment and exact net revenue |
| CASE-010 | Invoice and month/region target grains; conformed dimensions; calendar month derivation; attainment ratio | Aggregate transactional actuals before fact alignment |
| CASE-011 | Bridge schema and endpoint relationships; exact weights, total, tolerance and normalization scope; allocated metric reference | Weight validation, allocation and exact campaign totals |
| CASE-012 | Instant event time; Gregorian/UTC calendar-month derivation | Time bucketing and exact monthly totals |
| CASE-013 | Historical composite key; event/validity roles; closed-open interval; exactly-one temporal match declaration | As-of matching and overlapping/missing-history guards |
| CASE-014 | Nullable amount; distinct SUM, COUNT(value), COUNT(rows) null contracts | SQL null-elision and exact aggregate results |
| CASE-015 | Mandatory dataset policy; typed required sensitive context slot; boolean predicate | Trusted tenant binding, policy placement and non-broadening |
| CASE-016 | Billing and shipping roles coexist over the same customer key domain; model compiles | Query rejection with `AMBIGUOUS_RELATIONSHIP_PATH` when no role is selected |
| CASE-017 | USD and BOB metrics are individually valid; model compiles; a separate regression rejects a CSMIR metric that adds them | Query-created combination rejected by `sef-correctness` with `INCOMPATIBLE_UNITS` |
| CASE-018 | Base and rollup semantic schemas; declared grains; conformed dimensions; revenue identity and fingerprints | Materialization registration, substitution/equivalence proof and exact result comparison |
| CASE-019 | Exact `Decimal(18,4)`; absolute instant; Gregorian/America/La_Paz day derivation | Timezone execution, pinned TZDB membership and cross-engine numerical equivalence |
| CASE-020 | Semi-additive balance; additive account axis; forbidden month-end axis; no invented temporal selection; model compiles | Query rejection with `ILLEGAL_TEMPORAL_REAGGREGATION` |
| CASE-021 | Daily base metric; ordered cumulative metric; daily prerequisite grain; global partition; explicit order/frame | Daily aggregation followed by running sum |

The three rejected corpus queries (016, 017 and 020) therefore have **valid model
fixtures** in this stage. No query object or query correctness engine is introduced.
All declared relationship, uniqueness, temporal-match and allocation evidence starts
as `DeclaredUnverified`; this is sufficient for model compilation and is not a
certification claim. Additional regressions reject `Invalid` temporal-match and
allocation evidence, and definition-local incompatible currency arithmetic.

The fixture schemas make implementation choices where the corpus intentionally
does not specify an IR: integer IDs are signed 64-bit values; amounts use exact
`Decimal(18,2)` except CASE-019's required `Decimal(18,4)`; input month tokens denote
semantic Gregorian calendar-month start dates after future source decoding. Only
CASE-017 declares a currency, because other fixtures do not name one. The pinned
`2025b` TZDB identifier is a declared semantic fixture value, not a database shipped
or executed by this crate. The derived temporal definitions are CSMIR declarations;
source parsing, temporal execution and certification are deferred.

Phase 1A coverage is **21/21 model representations**. Full normative result/error,
planner and certificate coverage is **not run in Phase 1A**, and remains a later
authorized work package. No Phase 1B work is included here.
