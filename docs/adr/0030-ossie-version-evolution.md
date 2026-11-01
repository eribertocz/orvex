# 0030 — Evolving with Apache Ossie: version registry, staged reading and three identities

**Status:** Proposed (2026-11-01). Owner direction recorded in the conversation of 2026-11-01: plan A1–A4 and B1 approved.

This record amends **ADR-0009**. "The supported Ossie specification is pinned to an exact commit" becomes "the supported revisions are listed in a version registry, each identified by its commit and hashes". The rest of ADR-0009 stands: the model is a native Ossie document, `ORVEX` extensions carry what Ossie lacks, all Ossie knowledge lives in the Ossie adapter, and export reports every loss. Adding or changing a registry entry still needs an issue, and an ADR when the semantics change.

## Context
- **Ossie evolves quickly and has no release yet.**
  - Its schema requires `version: "0.2.0.dev0"`, and the same development number has already covered different revisions of the text and of the official validator.
  - Upstream added a SQL query interface (Layer 2), a declarative query interface (Layer 3, proposed) and an ontology layer (Layer 4).
  - Its roadmap standardizes concepts that are `ORVEX` extensions today: aggregation method, grain and entity, explicit cardinality, semantic filters, derived and cumulative metrics, units and formats.
- **The approved specifications assume exactly one version.** #278 (P2-03) rejects any version but the pinned one. #280 (P2-04) fingerprints a model without naming the Ossie revision that read it.
- **#278 named `DUCKDB` as an upstream dialect token.** It is not one: the upstream catalogs of dialects and vendors are community lists, not normative, and neither `DUCKDB` nor `ORVEX` is in them.
- **The project writer already preserves documents byte for byte** (ADR-0024): text-span edits, comments and unrelated text kept.

## Decision

### 1. Version registry
- **One registry** in `vendor/apache-ossie/`. Each entry records:
  - an `id`, the `specification_version` it reads, the upstream commit, and the SHA-256 of every vendored file (schema, `spec.md`, `expression_language.md`, the dialect and vendor catalogs);
  - a `state`:
    - `stable`: compiled and executed;
    - `experimental`: compiled and executed only in a project that opts in;
    - `read_only`: preserved and validated, never compiled;
    - `rejected`;
  - the `reader` that interprets it;
  - the reader's `capabilities` version.
- **A development number is not an identity.** Two revisions with the same `specification_version` are different entries.
  - At most one entry per number is active at a time; the registry check refuses two.
  - The others stay in the registry, so an old compiled model or receipt can still name the entry that read it.
- **Readers are reused across compatible revisions.** A new reader is written only when a revision changes the meaning of something the engine reads. A revision that changes only text, examples or validator wording reuses the reader and its capabilities version.
- **The first registry has one entry:** the current pin (`c721ffc`), `stable`. Orvex's own dialect tokens (`DUCKDB`, and the product's token from `identity.toml`) are listed beside the upstream catalogs. Recognizing a token never depends on an upstream registration.

### 2. Five stages, kept apart
1. **Preservation.** The document's bytes are kept as written. Nothing in Orvex rewrites what it does not own (ADR-0024). Unknown and foreign content survives every edit.
2. **Structural validation**, against the schema of the registry entry that matches the document's `version`. An unknown version → `UNSUPPORTED_OSSIE_VERSION`.
3. **Semantic validation:** names, references, the official validator's checks as errors, the `ORVEX` payloads (#278).
4. **Compilation**, into the version-independent `CompiledModel` (#280). The core never sees an Ossie version, only the reader's output.
5. **Execution**, only for an entry that is `stable`, or `experimental` in a project that opted in.

A document can pass stage 1 and fail stage 2 or stage 5. It can still be opened, preserved and exported, never executed.

### 3. Unknown capabilities fail closed
- **Inside `ORVEX` entries**, an unknown member is an error. The contract is closed (`ossie-extensions/v1`).
- **A standard capability the reader does not implement**, such as a newer construct in a `read_only` or `experimental` revision, or an expression form the reader cannot type, is rejected with a typed error. It is never skipped (`OSSIE_INVALID`, rule `unsupported_capability`).
- **Foreign extensions** (other vendors' `custom_extensions`) are preserved and never interpreted. Because they may change a metric, a filter, a relationship, a permission or a result in the tool that wrote them, they are not silently ignored either:
  - the compiled model lists every foreign extension and the element it sits on;
  - a query whose lineage reaches such an element is rejected until the Builder declares that vendor's extensions non-semantic for that project, through a reviewed, versioned declaration in the model;
  - the receipt lists the declaration that was relied on.

### 4. Standard fields and `ORVEX` extensions
When a registry entry's reader understands a standard concept that an `ORVEX` member also declares, precedence goes by **semantic relation, not representation**. The reader maps the standard field onto the engine's semantic aspects (for additivity: the aggregation class, the non-additive axes and their behavior, the period-end rule, null treatment). It then compares, aspect by aspect:

| Relation | Meaning | Result |
|---|---|---|
| equal | every aspect that either side defines agrees | accepted; the `ORVEX` member is reported as redundant (export may drop it) |
| refines | the extension defines aspects the standard leaves open, and agrees on every shared aspect | accepted; the extension stays authoritative for those aspects |
| conflicts | some shared aspect differs | rejected (`OSSIE_INVALID`, rule `extension_conflict`) |
| incomparable | the reader cannot decide an aspect | rejected; never assumed equal |

- Equal compiled trees are not enough: two different spellings of the same meaning must be `equal`, and two equal-looking declarations that differ in null handling, zone or frame must not be.
- Each mapping has positive and negative tests.
- Until a reader understands the standard concept, the `ORVEX` member is the only source, as today. `ossie-extensions/v1` stays stable.

### 5. Migration and export
- **Moving a document to another revision is always explicit.** It runs through the project writer, shows a diff and is confirmed by the Builder. It is never automatic on load or save.
- A migration is reversible when it loses nothing; otherwise it states its losses before confirmation.
- **Export accepts a target registry entry.** It reports every loss, and refuses a conversion that would change the meaning (P2-10).

### 6. Three identities
| Identity | Covers | Changes when | Used for |
|---|---|---|---|
| **Document** | SHA-256 of the file's bytes | any edit, comments and formatting included | the writer's conflict checks, audit, preservation tests |
| **Compiled model** | P2-04 `semantic_fingerprint`, plus the reader's capabilities version and the compiler rules version | a change in meaning: a definition, a reader whose capabilities changed, a compiler rule | model caches, catalog, impact analysis |
| **Execution** | compiled model + query + security context + data version + dialect adapter version | anything that can change a number | result cache key (ADR-0012), receipts (P2-07) |

- **Edits that cannot change meaning do not invalidate results.** A comment, a reordering or a new revision whose reader and capabilities version stay the same leaves the compiled identity unchanged.
- **Anything that can change meaning does invalidate.** Correctness wins.
- The registry entry's `id` is recorded in the compiled model and the receipt, for traceability, even when it does not change the identity.

### 7. Watching upstream
- A weekly, read-only workflow compares the vendored files and a list of watched upstream proposals with `apache/ossie`. It classifies each change as informational, compatible, potentially incompatible or semantically critical.
- It never fails a pull request, never changes a version or a contract, and never opens an issue.
- A change touching expressions, nulls, aggregates, relational semantics or the schema is flagged for a critical review; the classification is a warning, not a verdict.
- Adopting a revision is a normal issue (and an ADR when semantics change), tested on the corpus, the security suite, round-trip preservation and, when available, the upstream compliance suite.

## Alternatives considered
- **Keep one exact pin (ADR-0009 as written).** Simplest, but every upstream revision would break every model until a manual migration, and the version number cannot even tell revisions apart.
- **One full adapter per revision.** Isolates revisions, but duplicates code for revisions that change nothing the engine reads.
- **Follow upstream automatically.** Fast, but lets unapproved upstream changes reach production results.
- **Ignore foreign extensions, as the Ossie text allows.** Interoperable, but a foreign declaration that changes a metric in its own tool would silently give different numbers here.
- **Precedence by textual or tree equality.** Easy to implement, but it accepts declarations that differ in meaning and rejects ones that differ only in form.

## Consequences
- P2-03 (#278) resolves versions through the registry, keeps a reference to the preserved document and to the registry entry used, takes dialect tokens from the registry, and records foreign extensions. Its scope does not grow beyond that. Executing and refusing execution belong to later issues.
- P2-04 (#280) adds the reader's capabilities version to the compiled identity. Per the Owner's decision, it also rejects at compile time a metric whose aggregation is ambiguous without its declarations.
- `semantic-error/v1` gains the rules `unsupported_capability` and `extension_conflict` (compatible additions). The rejection of queries that reach unreviewed foreign extensions is added with its code when its issue is specified.
- A second registry entry, the full migrator and an extended compatibility matrix are built when a second revision is needed. The registry, the stages and the identities exist from P2-03, so that work adds entries, not a redesign.
- Contributing `ORVEX` and `DUCKDB` to the upstream catalogs is prepared separately and published only with the Owner's approval. Nothing here depends on it.
