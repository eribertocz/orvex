# `semantic-error` contract v1

**Producer:** `semantic` (`orvex.semantic.SemanticError`). **Consumers:** `server` (HTTP mapping, P2-27), `web` and `mcp`, which show the rendered message.

Every rejection, warning or notice of the semantic engine (`docs/architecture/semantic-engine.md` §12): a stable code, its fixed severity and category, a message rendered in one language for one audience, and typed details (laws 2, 3 and 11; anti-pattern A6).

## Files

| File | Holds |
|---|---|
| `schema.json` | The message: one closed variant per code and audience (below) |
| `codes.json` | Per code: `severity`, `category`, per detail field its disclosure (`public` or `builder`), and the fields a raise site may leave out (`optional`) |
| `messages/es.json`, `messages/en.json` | Per code: a `generic` and a `builder` template |
| `examples/` | A builder view of every code, an `other` view of every code with builder details, and one invalid example per rule |

`codes.json` and `messages/` are data: `tools/contracts/generate.py` copies them verbatim into the generated Python package (`semantic_error/v1_data/`), so the library and any future engine read the same catalog. A test checks that the schema, `codes.json` and both catalogs agree.

## The message

`kind` (`semantic_error`), `code`, `severity`, `category`, `audience`, `message`, `locale` (`es` or `en`) and `details`, all required; nothing else is allowed.

- **Severity and category are fixed per code.** Severity is `error`, except `DEPRECATED_METRIC` (`warning`) and the notices `FILTER_INTERSECTION_EMPTY`, `POLICY_ATTRIBUTE_MISSING` and `POLICY_MAPPING_MISSING` (the result has zero rows). The category (`model`, `query`, `correctness`, `security`, `budget`, `engine`) lets an answerability check report a rejection before execution (#239).
- **Details are a closed object per code**, every field required except those `codes.json` lists as `optional`. Exact numbers (counts, limits, estimates, lines) are decimal text without leading zeros. A detail holds names from the model or the query, enumerated values, counts and versions: never a data value, a secret or SQL.
- **Wording is not stable meaning.** A template may be reworded; the code, severity, category and details are the contract.

## Disclosure (#238 rule 12)

Each detail field is `public` or `builder` (`codes.json`).

- `audience: other`, the default and the answer for any unknown audience (fail closed), gets the code's `generic` template and only its public fields.
- `audience: builder` gets the `builder` template and every field.
- The schema enforces the pairing: a code with builder fields has two variants, one with `audience: builder` and every field, one with `audience: other` and only the public fields. So an `other` message that carries a builder field, or a builder message that lacks one, is invalid; the one exception is the optional `QUERY_BUDGET_EXCEEDED.estimate` (below).
- The audience decides only how much of the **error's explanation** is shown. It is never a data permission: a builder sees no data value, member or row that its own data policies hide. No administrative role is a builder audience by itself; administrative and data permissions are independent (Owner, 2026-11-01). Roles arrive with `security-context` (P2-09, P3); until then the audience is a parameter.
- **A public field names only what the caller may see.** A public name (a member, dataset, dimension or element) is always one the caller referenced or one visible to the caller. When the member at fault is not visible to the caller (a hidden base metric under a visible derived one, say), the raise site names the visible member the caller referenced, never the hidden one: a hidden member is never confirmed to exist (security.md §3.3). Every raise site (P2-03 onward) tests this.
- **Estimates and row policies (#238 rule 12).** `QUERY_BUDGET_EXCEEDED.estimate` is the only optional field. An estimate made from a whole table's statistics would reveal the size of rows a row policy hides, so a raise site leaves it out whenever the query reads a dataset with a row policy, for every audience: row policies bind builders too (Owner, 2026-11-01). The builder message then falls back to the generic template.
- `MEMBER_NOT_FOUND` names only what the caller wrote, so a hidden member and a missing one give the same message. `MEMBER_MASKED` carries no builder field: its builder message reveals nothing a policy hides. `POLICY_ATTRIBUTE_MISSING` and `POLICY_MAPPING_MISSING` give every other audience one generic sentence, which names no policy, table or value; only the builder view names the policy (Owner decision of 2026-11-02, #304). Until that date, `POLICY_ATTRIBUTE_MISSING` named its policy publicly. This change narrows what is shown, so no reader learns more than before.

## Messages

- A builder template that names an optional field left out falls back to the generic template.
- Templates use `{field}` placeholders, which must be detail fields of the code; a `generic` template uses public fields only.
- Rendering: text as it is; a list of names joined with `, `; a list of objects (`OSSIE_INVALID.problems`) as its count. A value is inserted as text and never read as a template.
- Locale: `es` by default; `en` on request; any other locale (or only its primary subtag, `en-US` → `en`) renders `es`, and `locale` reports the language actually used.
- No template holds the product name.

## Codes

| Code | Severity | Category | Details |
|---|---|---|---|
| `AMBIGUOUS_RELATIONSHIP_PATH` | error | query | `from`, `to`, `roles` (builder) |
| `AS_KNOWN_AT_UNSUPPORTED` | error | query | `dataset` |
| `BRIDGE_WEIGHTS_INVALID` | error | correctness | `bridge`, `groups` (builder) |
| `CONTEXT_CONFLICT` | error | query | `scopes` (builder) |
| `COVERAGE_POLICY_REQUIRED` | error | correctness | `dimension` |
| `CROSS_ENGINE_QUERY` | error | engine | `datasets`, `engines` (builder) |
| `DEPRECATED_METRIC` | warning | model | `metric` |
| `DIVISION_POLICY_VIOLATION` | error | correctness | `metric` |
| `DUPLICATE_COLUMN_POLICY` | error | model | `member` (builder) |
| `ENGINE_VERSION_UNTESTED` | error | engine | `engine`, `found` (builder), `tested` (builder) |
| `EVIDENCE_VIOLATED` | error | correctness | `dataset`, `evidence`, `fields` (builder), `violations` (builder) |
| `FANOUT_NOT_ISOLATABLE` | error | correctness | `metric`, `relationship` (builder) |
| `FILTER_CYCLE` | error | query | `filters` (builder) |
| `FILTER_INTERSECTION_EMPTY` | notice | query | `member` (builder) |
| `ILLEGAL_GRAIN_TRANSITION` | error | correctness | `metric`, `grain` (builder) |
| `ILLEGAL_TEMPORAL_REAGGREGATION` | error | correctness | `metric`, `axis` (builder) |
| `INCOMPATIBLE_UNITS` | error | correctness | `units` |
| `MATRIX_TOO_WIDE` | error | budget | `limit`, `maximum`, `requested` (builder) |
| `MEMBER_MASKED` | error | security | `member`, `operation` |
| `MEMBER_NOT_FOUND` | error | query | `member` |
| `METRIC_ALIGNMENT_AMBIGUOUS` | error | model | `metric`, `missing` |
| `METRIC_CYCLE` | error | model | `cycle` (builder) |
| `METRIC_IN_USE` | error | model | `metric`, `dependents` (builder) |
| `MISSING_ADDITIVITY_DECLARATION` | error | model | `metric` |
| `MISSING_CARDINALITY_DECLARATION` | error | model | `relationship` (builder) |
| `MISSING_EXTENSION_DECLARATION` | error | model | `element`, `name`, `declaration` |
| `NO_RELATIONSHIP_PATH` | error | query | `from`, `to` |
| `OSSIE_INVALID` | error | model | `file`, `problems` (builder), `problems_omitted` (builder) |
| `PAGE_CURSOR_INVALID` | error | query | `reason` |
| `PARAMETER_INVALID` | error | query | `parameter`, `expected` |
| `POLICY_ATTRIBUTE_MISSING` | notice | security | `policy` (builder) |
| `POLICY_INVALID` | error | model | `file`, `problems` (builder), `problems_omitted` (builder) |
| `POLICY_MAPPING_MISSING` | notice | security | `policy` (builder) |
| `POLICY_NOT_SUPPORTED_ON_ENGINE` | error | security | `dataset`, `policy` (builder), `reason` (builder) |
| `QUERY_BUDGET_EXCEEDED` | error | budget | `budget`, `limit`, `estimate` (builder, optional) |
| `QUERY_INVALID` | error | query | `problems` (builder), `problems_omitted` (builder) |
| `REAGGREGATION_NOT_ALLOWED` | error | correctness | `metric`, `class` |
| `TEMPORAL_MATCH_NOT_UNIQUE` | error | correctness | `relationship` (builder), `violations` (builder) |
| `UNSUPPORTED_OPERATION` | error | engine | `operation` |
| `UNSUPPORTED_OSSIE_VERSION` | error | model | `found`, `supported` |
| `WINDOW_UNDEFINED_AT_TOTAL` | error | query | `metric`, `level` |

41 codes: the 27 of §12 (with `MISSING_EXTENSION_DECLARATION`, formerly a name containing the product name), S7 `METRIC_ALIGNMENT_AMBIGUOUS` and `METRIC_CYCLE`, S8 `CONTEXT_CONFLICT`, #238 rule 3 `FILTER_CYCLE`, S9 `WINDOW_UNDEFINED_AT_TOTAL` (also a cell state, carried by P2-06), S12 `AS_KNOWN_AT_UNSUPPORTED`, S10 `PARAMETER_INVALID` and S11 `REAGGREGATION_NOT_ALLOWED`, P2-05's `QUERY_INVALID` (#283), P2-06's `PAGE_CURSOR_INVALID` and `FILTER_INTERSECTION_EMPTY` (#293), and P2-08's `POLICY_INVALID`, `DUPLICATE_COLUMN_POLICY` and `POLICY_MAPPING_MISSING` (#304). S13 adds none (the optimizer never rejects; it keeps the base plan), and S14 adds none (repairs ride on other codes).

`OSSIE_INVALID.problems` holds 1–50 problems (`pointer`, a JSON Pointer; `line`; `rule`: `syntax`, `schema`, `duplicate_name`, `reference`, `extension`, `extension_duplicate`, `extension_conflict` (a standard field and an `ORVEX` declaration disagree, ADR-0030 §4), `unsupported_capability` (a capability the reader does not implement, or a revision that may not run, ADR-0030 §3), `expression` (an OSSIE_SQL_2026 text that is not one SQL expression, or that the specification forbids: empty, unbalanced, more than one statement, an identifier over 128 characters; or a product expression that cannot be parsed), `type` (an expression whose types do not agree, P2-04)), and `problems_omitted` how many more were found. `QUERY_INVALID.problems` holds 1–50 problems (`pointer`, a JSON Pointer into the query; `rule`: `syntax` (not strict JSON, a repeated key included), `schema` (not a `semantic-query` v1 document), `kind` (a member of the wrong kind for its slot), `zone` (a relative date with no zone to resolve it in)), and `problems_omitted` how many more were found. `POLICY_INVALID.problems` holds 1–50 problems (`pointer`, a JSON Pointer into the policy file; `line`; `rule`: `syntax`, `schema`, `duplicate_name`, `reference`, `predicate` (outside the closed grammar), `registration` (a deny-list without one), `inference` (a model element exposing a protected member), `propagation` (too many paths to report), `masked_metric`, `limits`), and `problems_omitted` how many more were found. `PAGE_CURSOR_INVALID.reason` is `expired` when the cursor's snapshot is no longer available, and `invalid` for every other refusal (malformed, forged, altered, unknown, or presented with another caller, query, model, page size, permissions or data version), so nothing says which binding failed. `MEMBER_MASKED.operation` is `group_by`, `filter`, `sort`, `distinct_count` or `possible_values`.

## Compatibility

- Every v1 code is stable (ADR-0004 rule 6).
- **Compatible:** a new code (a new enum value and its variants, entries in `codes.json` and both catalogs), a new value of a detail's enumeration (such as `OSSIE_INVALID` `rule`), a reworded template, and a new optional detail field, which is compatible for readers of messages (a producer of v1 messages never sends it).
- **Incompatible:** removing or renaming a code; changing its meaning, severity or category; changing a field's disclosure in either direction (the `other` variants are closed and require every public field, so moving a field to `builder` breaks old messages and moving one to `public` discloses more); removing a field or making a new one required. These need `v2`, an ADR and a transition period.

## Reserved: `repairs` (S14)

The name `repairs` is reserved. The S14 issue adds it as an optional field, a compatible change, with its approved rules: at most three, in a deterministic order, never applied automatically, never naming a hidden member. It is not in v1: a repair is an alternative `semantic-query` (P2-05), and contracts allow only local references and closed objects. A v1 message with `repairs` is invalid.
