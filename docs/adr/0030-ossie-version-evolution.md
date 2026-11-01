# 0030 — Evolving with Apache Ossie: version registry, staged reading and three identities

**Status:** Proposed (2026-11-01). Owner direction recorded in the conversation of 2026-11-01: plan A1–A4 and B1 approved. The Ossie-first policy (§8) and the separation of conformance and executability (§2) were approved by the Owner on 2026-11-01.

This record amends **ADR-0009** and **ADR-0012**.

**ADR-0009.** "The supported Ossie specification is pinned to an exact commit" becomes "the supported revisions are listed in a version registry, each identified by its commit and hashes". The rest of ADR-0009 stands: the model is a native Ossie document, `ORVEX` extensions carry what Ossie lacks, all Ossie knowledge lives in the Ossie adapter, and export reports every loss. Adding or changing a registry entry still needs an issue, and an ADR when the semantics change.

**ADR-0012.** The result-cache key keeps its four parts (query fingerprint, model fingerprint, data snapshot, permission fingerprint). It gains the engine's version and the dialect adapter's version (§6), because the engines disagree on exact arithmetic (#280) and an adapter change can change a number.

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
  - the `reader` that interprets it (a stable reader id);
  - the reader's `capabilities` version.
- **Active entries.** The active entry for a `specification_version` is the one that reads documents declaring it. Only a `stable`, `experimental` or `read_only` entry can be active; a `rejected` one never is.
- **A development number is not an identity.** Two revisions with the same `specification_version` are different entries.
  - At most one entry per number is active at a time; the registry check refuses two.
  - The others stay in the registry, so an old compiled model or receipt can still name the entry that read it.
- **Readers are reused across compatible revisions.** A new reader is written only when a revision changes the meaning of something the engine reads.
  - A revision whose changes are **editorial only** reuses the reader and its capabilities version: typos, examples, formatting, links, or validator messages with the same verdicts.
  - The text of `spec.md`, `expression_language.md` and `relational_semantics.md` is normative. The adoption issue classifies every normative diff, with evidence. When in doubt, the capabilities version is raised.
- **The first registry has one entry:** the current pin (`c721ffc`), `stable`. Orvex's own dialect tokens are listed beside the upstream catalogs: `DUCKDB`, `CLICKHOUSE` (registered upstream only after the pin, in `2fd19c7`) and the product's token from `identity.toml`. An upstream validator warning about a dialect it does not know is not promoted to an error for these tokens. Recognizing a token never depends on an upstream registration.

### 2. Five stages, kept apart
1. **Preservation.** The document's bytes are kept as written. Nothing in Orvex rewrites what it does not own (ADR-0024). Unknown and foreign content survives every edit.
2. **Structural validation**, against the schema of the registry entry that matches the document's `version`. An unknown version → `UNSUPPORTED_OSSIE_VERSION`.
3. **Semantic validation:** names, references, the specification's rules and the official validator's errors as errors, the `ORVEX` payloads (#278).
4. **Compilation**, into the version-independent `CompiledModel` (#280). The core never sees an Ossie version, only the reader's output.
5. **Execution**, only for an entry that is `stable`, or `experimental` in a project that opted in.

A document can pass stage 1 and fail stage 2 or stage 5. It can still be opened, preserved and exported, never executed.

**Conformance and executability are separate.**
- Stages 2 and 3 decide only conformance with Ossie. A document Ossie accepts is never refused there for a rule stricter than Ossie's. An official validator warning, such as `to_columns` covering no key of `to`, stays a warning.
- Where the product needs more to compile or execute safely, stage 3 records an advisory on the element. Examples: a key or an as-of declaration for that relationship, or a selectable expression for a field.
- Stage 4 or 5 refuses, with a typed error, the operation that needs the element. It never returns a result that could be wrong.

### 3. Unknown capabilities fail closed
- **Inside `ORVEX` entries**, an unknown member is an error. The contract is closed (`ossie-extensions/v1`).
- **A standard capability the reader does not implement**, such as a newer construct in a `read_only` or `experimental` revision, or an expression form the reader cannot type, is rejected with a typed error. It is never skipped (`OSSIE_INVALID`, rule `unsupported_capability`).
- **Foreign extensions** (other vendors' `custom_extensions`) are preserved and never interpreted. Ossie says only that it carries them without interpreting them; it does not say they are inert. Because they may change a metric, a filter, a relationship, a permission or a result in the tool that wrote them, they are not silently ignored either:
  - **Listing.** The compiled model lists every foreign entry with its element and the SHA-256 of its `data`.
  - **Reach.** A query is refused when any element it uses carries an unreviewed entry. The elements it uses are: the model root (so an entry on the model blocks every query); every dataset it reads, including datasets reached through relationships, bridges and policies; every relationship it traverses; and every field and metric it references, directly or through a dependency.
  - **Review.** The Builder reviews one entry at a time. The declaration names the element, the vendor and the `data` hash, and sits in the model itself, so it is versioned and reviewed in Git. A new or changed entry has no declaration, so it is refused until reviewed; a declaration never covers a whole vendor.
  - **Receipt.** It lists the declarations relied on.
  - **First execution.** The refusal ships with the first issue that executes queries (P2-12 in the roadmap). No execution path exists without it. That issue adds the declaration as an optional `ORVEX` member, a compatible change to `ossie-extensions/v1`.

### 4. Standard fields and `ORVEX` extensions
When a registry entry's reader understands a standard concept that an `ORVEX` member also declares, precedence goes by **semantic relation, not representation**. The reader maps the standard field onto the engine's semantic aspects (for additivity: the aggregation class, the non-additive axes and their behavior, the period-end rule, null treatment). It then compares them aspect by aspect. Each aspect falls into exactly one case, and a standard default counts as defined:

| Aspect | Result for that aspect |
|---|---|
| defined by both, and they agree | accepted |
| defined by both, and they differ | **conflict** |
| defined only by the standard | accepted; the standard decides it |
| defined only by the extension | accepted; the extension decides it (a refinement) |
| the reader cannot decide whether they agree | **undecidable** |

- **Declaration level.** A declaration is accepted only if no aspect is a conflict or undecidable; anything else is rejected (`OSSIE_INVALID`, rule `extension_conflict`). A pair that is not covered is never assumed equal.
- **Redundant members.** When every aspect is defined by both and agrees, the `ORVEX` member is reported as redundant, and export may drop it.

- Equal compiled trees are not enough: two different spellings of the same meaning must agree on every aspect, and two equal-looking declarations that differ in null handling, zone or frame must not be.
- Each mapping has positive and negative tests.
- **When the standard field is absent**, the `ORVEX` member is the only source, as today. A standard field that is present but that the reader does not understand is an unknown capability (§3) and is rejected; it is never skipped in favour of the extension.
- `ossie-extensions/v1` stays stable.

### 5. Migration and export
- **Moving a document to another revision is always explicit.** It runs through the project writer, shows a diff and is confirmed by the Builder. It is never automatic on load or save.
- A migration is reversible when it loses nothing; otherwise it states its losses before confirmation.
- **Export accepts a target registry entry.** It reports every loss, and refuses a conversion that would change the meaning (P2-10).

### 6. Three identities
| Identity | Covers | Changes when | Used for |
|---|---|---|---|
| **Document** | SHA-256 of the file's bytes | any edit, comments and formatting included | the writer's conflict checks, audit, preservation tests |
| **Compiled model** | P2-04 `semantic_fingerprint`, plus the reader's id and capabilities version and the compiler rules version | a change in meaning: a definition, another reader or a reader whose capabilities changed, a compiler rule | model caches, catalog, impact analysis |
| **Execution** | compiled model + query fingerprint + permission fingerprint + data snapshot or version + engine version + dialect adapter version | anything that can change a number | result cache key (ADR-0012 as amended above), receipts (P2-07) |

- **Edits that cannot change meaning do not invalidate results.** These leave the compiled identity unchanged:
  - a comment;
  - a reordering of an unordered collection (an ordered list, such as `group_by` or a drill path, is meaning);
  - a new editorial revision read by the same reader at the same capabilities version.
- **Anything that can change meaning does invalidate.** Correctness wins.
- The registry entry's `id` is recorded in the compiled model and the receipt, for traceability, even when it does not change the identity. A receipt flags a result read through an `experimental` entry.

### 7. Watching upstream
- A weekly, read-only workflow (#285) compares the vendored files and a list of watched upstream proposals with `apache/ossie`. It classifies each change as informational, compatible, potentially incompatible or semantically critical.
- It never fails a pull request, never changes a version or a contract, and never opens an issue.
- A change touching expressions, nulls, aggregates, relational semantics or the schema is flagged for a critical review; the classification is a warning, not a verdict.
- Adopting a revision is a normal issue (and an ADR when semantics change), tested on the corpus, the security suite, round-trip preservation and, when available, the upstream compliance suite.

### 8. Ossie-first, with explicit extensions
- **Ossie is the authority** for every capability the pinned registry entry specifies. Its representation is the one the product reads, and its rules are never relaxed to accept a model.
- **Product extensions are explicit and versioned.** A capability Ossie does not yet specify uses an `ORVEX` entry of `ossie-extensions` (versioned by the contract), kept apart from standard semantics. It is never presented as an Ossie feature. An invalid standard expression is never read as an extension: an extension applies only where it is declared. For example, an expression over metrics is a derived metric (`over_metrics` or `window`) with its text under the product's dialect token.
- **The standard takes precedence once it exists.** When Ossie specifies an equivalent capability, new models use the standard representation. Equivalence is first verified aspect by aspect (§4).
- **Earlier extensions are not removed automatically.** They stay readable through a controlled transition. Moving a document to the standard form is an explicit migration through the project writer (ADR-0024, §5): it shows its differences and is refused if a meaning or a result could change.
- **Explicit declarations, nothing inferred.** A capability that needs information Ossie does not carry is refused until that information is declared: an alignment grain, a dependency, a window, an as-of match. The issues that build these capabilities require the declarations: P2-13, P2-17 and P2-20 for the corpus's derived, combined and cumulative metrics.
- **The capability matrix** in `docs/architecture/ossie-capabilities.md` records, for each capability: the standard, the extension, the upstream status (§7) and the migration state.

## Alternatives considered
- **Keep one exact pin (ADR-0009 as written).** Simplest, but every upstream revision would break every model until a manual migration, and the version number cannot even tell revisions apart.
- **One full adapter per revision.** Isolates revisions, but duplicates code for revisions that change nothing the engine reads.
- **Follow upstream automatically.** Fast, but lets unapproved upstream changes reach production results.
- **Refuse at load what the product cannot yet execute.** Simpler, but it calls documents invalid that Ossie accepts, and confuses fidelity to the standard with the correctness of results.
- **Ignore foreign extensions, as the Ossie text allows.** Interoperable, but a foreign declaration that changes a metric in its own tool would silently give different numbers here.
- **Precedence by textual or tree equality.** Easy to implement, but it accepts declarations that differ in meaning and rejects ones that differ only in form.

## Consequences
- P2-03 (#278) resolves versions through the registry, keeps a reference to the preserved document and to the registry entry used, takes dialect tokens from the registry, records foreign extensions, and records advisories where a conformant document is not yet executable. Its scope does not grow beyond that. Executing and refusing execution belong to later issues.
- P2-04 (#280) adds the reader's id and capabilities version to the compiled identity. Per the Owner's decision, it also rejects at compile time a metric whose aggregation is ambiguous without its declarations.
- `semantic-error/v1` gains the rules `unsupported_capability` and `extension_conflict`. These are compatible additions to a detail's enumeration, and its README's compatibility list says so explicitly when P2-03 adds them. The refusal of queries that reach unreviewed foreign extensions gets its code in P2-12.
- `UNSUPPORTED_OSSIE_VERSION.supported` names the newest `stable` version. A list of every supported version, when there is more than one, is an optional detail (compatible).
- A second registry entry, the full migrator and an extended compatibility matrix are built when a second revision is needed. The registry, the stages and the identities exist from P2-03, so that work adds entries, not a redesign.
- Contributing `ORVEX` and `DUCKDB` to the upstream catalogs is prepared separately and published only with the Owner's approval. Nothing here depends on it.
