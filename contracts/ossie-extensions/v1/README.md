# `ossie-extensions` contract v1

**Producer:** whoever writes a semantic model: the modeler, by hand or through the Studio (P5, P8). **Consumers:** `semantic`, whose Ossie adapter (P2-03) decodes and checks each payload, and `conformance`, whose corpus adapter translates the imported corpus into it.

The product's declarations on an [Apache Ossie](https://github.com/apache/ossie) semantic model: the meaning Ossie does not carry and that the semantic engine never assumes (`docs/architecture/semantic-engine.md` §3). This schema validates the payload of one `custom_extensions` entry.

## Envelope

- An Ossie `custom_extensions` entry is `{vendor_name, data}`, and `data` "is an opaque string that Ossie carries but does not interpret" (Ossie `core-spec/spec.md` L597, pinned commit `c721ffc`).
- The product's entries use the vendor name in `identity.toml` (`ossie_vendor_name`; older models may still use one of `previous_vendor_names`). The name is read at run time and never written in this contract.
- Their `data` is JSON text of **one object**, and this schema validates that decoded object, not the string. If Ossie makes `data` an object (upstream PR #522), only the decoding in P2-03 changes; this contract stays v1.
- `version` (const `1`) and `element` are required. `element` is the Ossie element the entry is attached to:
  - `model`, `dataset`, `field`, `metric` or `relationship`;
  - P2-03 checks that it matches;
  - Ossie `Dimension` takes no extensions, so dimension semantics sit on the `Field`.
- At most one product entry per element. The current vendor name together with a previous one on the same element is rejected (P2-03).

## What Ossie already carries (never repeated here)

| Ossie | Use it for |
|---|---|
| `Dataset.primary_key`, `unique_keys` | keys; `entity_keys` here must equal one of them unless `coarser: true` |
| `Field.datatype`, `Dimension.is_time` | logical type and time role; `time_roles` here names fields whose effective `is_time` is `true` |
| `FILTER (WHERE …)` in a metric expression | intrinsic filters, which "MUST NOT" be rewritten (`expression_language.md` L265) |
| `Relationship.from` / `to` | direction: `from` is the many side |
| `description`, `ai_context`, `label` | documentation and AI context |

## Members

Every member is optional unless marked required; ⇒ marks a condition the schema enforces.

**Model (`element: model`).**
- `default_time_zone`: an IANA name.
- `calendars`: `gregorian`, `fiscal` (⇒ `year_start_month`) or `business` (⇒ `dataset`, `date`, `business_day`).
- `drill_paths`: at least two levels each.
- `parent_child`.
- `calculation_groups`, with an explicit `precedence`.
- `parameters` (S10).
- `segments` (S10).
- `cohorts` and `funnels` (S11).
- `preaggregations` (S5, S13).

**Dataset (`element: dataset`).**
- `entity`, `entity_keys`.
- `grain`: conformed dimensions.
- `time_roles`: `event`, a closed–open `validity`, `snapshot`.
- `history` (S12): `versioned` ⇒ `entity_key`, `version`, `recorded_at`, `deleted`.
- `evidence`: `unique`, `non_overlap`, `append_only`.
- `cache`, `certification`.
- `aggregate_table` (S5): this dataset is an existing aggregate table at its `grain` ⇒ `grain`; its measures replace base execution only when proven equivalent (C8). The proof needs what the table stores, declared, never guessed from names (P2-25): `base_dataset`, the dataset it aggregates, and on each measure `metric`, the base metric it stores at the table's grain, with `expression` re-aggregating it (`SUM(column)`). Without them the table is never used.
- `bridge` (C7) on a bridge dataset. A many-to-many is two Ossie many-to-one relationships through it, `left` and `right`. `weight` allocates within `normalization_scope`, summing to `total` within `tolerance`.

**Field (`element: field`).**
- `semantic_type`, with these conditions:
  - `amount` ⇒ `currency` (ISO 4217) and no `unit`; `currency` appears only on an `amount`, and ⇒ `semantic_type`;
  - `quantity` ⇒ `unit` (UCUM syntax);
  - `geo_region` ⇔ `geo_source` (a `.geojson` under `assets/geo/`).
- `numeric`:
  - for `Decimal`, `precision` and `scale`;
  - for `Integer`, `width` and `signed`;
  - `precision`, `scale` and `width` as text and `signed` as a boolean, because Ossie leaves them unspecified (spec L74).
  - Which members a field's Ossie `datatype` requires or forbids (`numeric` on `Decimal` and `Integer`, `time_zone` and `calendar` on temporal types) depends on the Ossie document, so P2-03 checks it, not this schema.
- `time_zone`: on `DateTime`, the civil zone of origin; on `DateTimeTz`, the named zone Ossie does not keep (spec L80).
- `conformed_dimension`, `nullability`, `calendar`, `pii`.
- `calculated` (S10): an expression over fields of the same row, with a bounded cardinality.
- `business_time`: the field is a business-time bucket (a local day or month) derived in its `time_zone` ⇒ `time_zone`.

**Metric (`element: metric`).**
- `additivity`, with its conditions:
  - `semi_additive` ⇒ `non_additive_axes`, each axis a dimension or an entity, with `behavior` (`additive`, `forbidden`, `period_end`, `recompute`);
  - an axis whose behavior is `period_end` ⇔ `period_end` (its `order_by` and tie rule);
  - `recomputable` ⇒ `components`;
  - `non_additive_axes` and `period_end` appear only on `semi_additive`, `components` only on `recomputable`.
- `aggregate_class`: `distributive`, `algebraic`, `holistic` or `sketch`. P2-03 checks it against the aggregates of the Ossie expression (`expression_language.md` L281–L290).
  - `approximate` is allowed only on `sketch`.
  - `reaggregable_state` (`exact_bitmap`, `exact_sorted_set`) names an exact state that re-aggregates.
- `zero_denominator`, `rounding`, `unit`, `currency`, `format`, `owner`.
- `status`: `deprecated` ⇔ `deprecation`.
- `over_metrics` (S7): an expression over metrics with a required `align` on grain and time.
- `context` (S1, S8), `level_of_detail`: a `keep` or `remove` step ⇒ `targets`; `over_metrics.ignore_filters` names filters a derived metric does not inherit.
- `window` (S9): `partition_by`, `order_by` and `densify` are always explicit.
- `allocation`.
- `home_dataset`: the dataset a model metric that names no field (`COUNT(*)`) counts.
- `over_metrics` and `window` exclude each other.

**Relationship (`element: relationship`).**
- `cardinality`: `many_to_one` or `one_to_one`.
- `optionality`.
- `evidence`: `cardinality`, `referential_integrity`.
- `temporal`: an as-of match of an event time against a validity, with `boundary: closed_open` and `match: exactly_one`.
- A role is a separate Ossie relationship, named by its `name`.

## The product's expression language

`over_metrics.expression`, `components`, `calculated.expression`, calculation items, segment conditions and funnel steps are not plain Ossie expressions, which cannot refer to other metrics or fields:
- "Metric expressions refer to dataset fields. They cannot refer to columns of a dataset's `source` or to other metrics." (spec L424)
- "This version of the specification does not support: a field expression that refers to another field … a metric expression that refers to another metric" (spec L692–L699).

They use the OSSIE_SQL_2026 subset plus references:
- `dataset.field` for fields;
- `metric` for model metrics;
- `dataset.metric` for dataset metrics.

Identifiers follow Ossie's rule: at most 128 characters, compared case-insensitively unless quoted (`expression_language.md` L86–L88). P2-04 parses and types them, and rejects any cycle.

A derived metric still carries Ossie's required `expression`: its portable expansion when one exists, such as `SUM(a)/SUM(b)`; otherwise an entry in `expression.dialects` under the product's dialect token, read from `identity.toml`.

## Rules

- **Exact values.** Tolerances and error bounds are non-negative. Decimals, counts and anything that may exceed 2^53 are text with a decimal pattern; the only JSON numbers are `version` and small bounded integers (`year_start_month`, `rounding.scale`, `precedence`). Dates are ISO 8601, durations ISO 8601, zones IANA, currencies ISO 4217.
- **Declarations never replace verification.** Evidence status (`declared`, `verified`, `violated`) is engine state, never written here. A declared cardinality that the data violates yields `EVIDENCE_VIOLATED`; the engine never picks a "first matching row".
- **Fail closed.** An unknown member, a wrong type or an unmet condition is rejected, never ignored.
- **Units are declared and checked, never converted.** The transformation layer converts values; the engine rejects mixed units (`INCOMPATIBLE_UNITS`, P2-04).

## Compatibility

- Every v1 member is stable.
- **Compatible changes:** a new optional member, or a new documented enum value.
- **Incompatible changes:** removing, renaming or retyping a member, making one required, or removing an enum value. Any of these needs `v2`, an ADR and a transition period.
- `tests/contracts/ossie_extensions_v1_frozen.json` is the schema as released; `tests/contracts/test_ossie_extensions.py` compares every node of it with the current schema and fails on any incompatible change.

Names follow the upstream proposal for units and formats (Ossie PR #287), so a merged proposal maps without a rename:

| This contract | Ossie PR #287 |
|---|---|
| `unit` | `measurement` |
| `format` | `display_format` |
| `semantic_type` | `semantic_type` |

## Export correspondence (P2-10)

For the export's loss report, these members have direct counterparts in other open semantic formats.

| This contract | Counterpart |
|---|---|
| `non_additive_axes` | `NON ADDITIVE BY`; `non_additive_dimension` |
| `period_end` | `window_choice`; `semiadditive: first \| last` |
| `over_metrics`, `window`, `funnels` | derived, cumulative and conversion metric types |

## Examples

- `examples/valid/` holds one full payload per element, plus geo, S5, S7, S9, S10, S11 and S12 declarations.
- Each file in `examples/invalid/` breaks one rule. The schema reports a union mismatch at the top level, so each `.expected.txt` holds that message. The test file asserts the precise reason against the element's own schema.
