# Ossie capabilities: standard, extension, adoption

**Policy (Owner, 2026-11-01): Ossie-first, with evolving extensions.** This document applies ADR-0009 and ADR-0030 and the `ossie-extensions/v1` contract. It adds no mechanism of its own.

1. **Ossie comes first.** Apache Ossie is the primary standard. Every capability the pinned registry entry specifies is implemented strictly and has precedence.
2. **Extensions are explicit.** A capability Ossie does not yet specify is a product extension: an `ORVEX` entry of `ossie-extensions/v1`, explicit, versioned and kept apart from standard semantics. It is never presented as an Ossie feature.
3. **The standard wins once it exists.** When Ossie specifies an equivalent capability, new models use the standard representation, after semantic equivalence is verified (ADR-0030 §4).
4. **Old extensions stay readable.** They are not removed automatically: they stay compatible through a controlled transition, with explicit migration, a difference report, and refusal when a meaning could change (ADR-0030 §5).
5. **No silent reinterpretation.** An invalid standard expression is never read as an extension. An extension is used only where it is declared.
6. **Adoption is never automatic.** Upstream changes reach this matrix only through the watch (#285), an issue, a registry entry and tests.

## Matrix

Pinned revision: `c721ffc` (`0.2.0.dev0`). "Upstream" is what the watch tracks; it is not a commitment of the specification.

| Capability | Ossie at the pinned revision | Product extension (`ossie-extensions/v1`) | Upstream | Migration |
|---|---|---|---|---|
| Document shape, version, datasets, fields, metrics, relationships | specified; read strictly (P2-03) | — | — | — |
| Keys: `primary_key`, `unique_keys` | specified | `entity_keys` must equal one, unless `coarser` | — | — |
| Relationship direction (`from` is the many side) | specified | `cardinality` refines it; a conflict is refused | explicit cardinality is on the roadmap | none yet |
| `to_columns` covering a key of `to` | the official validator warns | the product refuses, unless a declared as-of match is coherent (`temporal`) | — | — |
| Data types; `is_time` | specified | `numeric` (precision, scale, width), `time_zone`, `calendar` | — | — |
| Expressions in OSSIE_SQL_2026; dialect entries | specified; `sql` is authoritative | — | — | — |
| A metric naming another metric (derived metric) | not supported (spec L424, L692–L699) | `over_metrics` or `window`, with the expression under the product's dialect token; for `over_metrics`, that entry and the payload state the same expression | derived and cumulative metrics are on the roadmap | none yet |
| Aggregation method, additivity | decomposability table (`expression_language.md` L281) | `additivity`, `aggregate_class`, `non_additive_axes`, `period_end` | aggregation method is on the roadmap | none yet |
| Grain and entity | — | `entity`, `entity_keys`, `grain` | on the roadmap | none yet |
| Semantic filters | — | `segments`, `context`, `level_of_detail` | on the roadmap | none yet |
| Units and formats | — | `unit`, `format`, `semantic_type` | Ossie PR #287 (`measurement`, `display_format`) | a mapped rename (`unit` → `measurement`, `format` → `display_format`) |
| `custom_extensions.data` | an opaque string | JSON text of one object | Ossie PR #522 (an object) | only the decoding changes |
| Other vendors' extensions | carried, not interpreted | listed, never interpreted; reviewed before execution (P2-12) | — | — |

## Corpus classification (P2-03)

| Cases | Class | Product representation |
|---|---|---|
| 16 models | accepted by P2-03's checks; the syntax and types of expressions are checked by P2-04 | — |
| CASE-006, -009, -010, -021 | not conformant: a metric names a metric | a derived metric. CASE-010's translation declares `over_metrics` from bi-platform's grain. The other three need a declaration that the corpus does not give: an alignment for -006 and -009, a `window` for -021. |
| CASE-013 | conformant Ossie, refused by the product's key-coverage rule | a declared as-of match (`temporal`) |

The imported files are never edited. Resolving dependencies, detecting cycles and executing derived metrics belong to P2-04 and later; P2-03 only reads, preserves and validates the declarations.
