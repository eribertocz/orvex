# `semantic-query` contract v1

**Defined by** `semantic` (`contracts.toml`), whose binder (`bind_query`, P2-05b) turns a query into one canonical, typed form or a `semantic-error` (#283). **Written by** the engine's callers: `server` (HTTP, P2-27), `web`, `mcp` and tests.

One closed, exact format for every question asked of the engine (`docs/architecture/semantic-engine.md` §4, "Query binding"). It carries the rules of #238 (filter scopes, metric filters, `NULL` handling) and represents S1, S2, S3, S10 and S12 from the start (ADR-0029 decision 4). It does not depend on the Ossie revision: an Ossie query interface, if adopted, is a front-end that translates into this format (#283 agent decision 4).

## Shape

Every object is closed: an unknown member is invalid. The only JSON number is `version`, and every other number is text. `NULL` is never a value.

| Member | Mode | Holds |
|---|---|---|
| `kind` | both | `semantic_query` |
| `version` | both | `1` |
| `mode` | both | `aggregate` or `detail`: two closed variants, each refusing the other's members |
| `metrics` | aggregate, required | 1–100 `{metric, context?, apply?}` |
| `group_by` | aggregate | 1–64 `{member, role?, bucket?}` |
| `metric_filters` | aggregate | 1–64 metric filters (below) |
| `contexts` | aggregate | 1–16 `{name, filters}` (S2) |
| `top_n` | aggregate | `{by, n, per?, others}` (S10) |
| `coverage` | aggregate | `aligned` or `full` (S3) |
| `limit` | aggregate | a count |
| `fields` | detail, required | 1–256 `{member, role?}` |
| `page` | detail, required | `{size, after?}` |
| `order` | both; detail: required | 1–64 `{by, direction?, nulls?}` |
| `filters` | both | 1–256 filters (below) |
| `parameters` | both | 1–64 `{name, value}` (S10) |
| `as_known_at` | both | an RFC 3339 timestamp with an offset (S12) |
| `context_modifiers` | both | 1–64 `{op, scope, members}` (#238 rule 3.3) |

### Aggregate mode
- **`metrics`**, in presentation order. An entry's `metric` is a metric. `context` names one of the query's `contexts`, so the metric is evaluated in that context (S2). `apply` lists calculation items (S1/S8, the model's `calculation_groups`) in the order they apply.
- **`group_by`**, in presentation order. `member` is a row-level member, and `role` names the relationship that reaches it. `bucket` is `{unit, calendar?}`, with `unit` one of `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year`; `calendar` names a model calendar. Only a temporal member takes a bucket.
- **`contexts`** (S2): named filter sets, each evaluated independently, aligned for comparison.
- **`top_n`** (S10): the first `n` members by the metric `by`, within each combination of `per` when given.
  - Ties are broken by the member's own value, ascending, so the result never depends on scan order.
  - `others: true` adds one "Others" row. Its value is recomputed over the remaining members, never summed from their values.
- **`coverage`** (S3): the explicit choice, `aligned` or `full`, for comparing a partial period with a complete one (`semantic-engine.md` §8 S3, `COVERAGE_POLICY_REQUIRED`). Its exact semantics are bound in P2-05b and P2-11.
- **`order`**: `by` names the metric or member to sort by. `direction` defaults to `asc` and `nulls` to `last`.
- **`limit`**: at most this many rows.

### Detail mode (`semantic-engine.md` §9)
- **`fields`**: row-level members in column order, each with an optional `role`.
- **`order`** is required. The engine appends the declared unique key as the final tie-breaker, so paging is stable.
- **`page`**: `size` rows, after the opaque cursor `after` returned with the previous page (`semantic-result` v1, P2-06). A size above the project's cap is refused when bound.

### Common to both modes
- **`filters`** (below).
- **`parameters`** (S10): a value for each named model parameter.
- **`as_known_at`** (S12): read the data as it was known at that instant. A dataset without `history` refuses it when bound.
- **`context_modifiers`** (#238 rule 3.3): the visual's declared exceptions to the filter context. `op` is `remove` or `keep`, applied to the named members' filters of that scope; binding and planning fix their exact effect (P2-05b onward). They are recorded in the receipt.

## Filters

A filter is `{scope, member, role?, predicate}`.
- **`scope`** is one of `board`, `page`, `visual`, `cross_filter`, `drill` (#238 rule 3).
  - Scopes combine with AND, in that order. An inner scope never replaces an outer one.
  - Two filters on one member in two scopes stay two filters: their meaning is the intersection, and an empty one gives zero rows, never an error.
  - Row policies and the model's intrinsic `FILTER`s always apply inside every scope.
- **`member`** is a row-level member, and **`role`** names the relationship that reaches it.
- **`predicate`** is one closed variant, chosen by `op`. Each variant refuses the others' members.

| `op` | Members | Meaning |
|---|---|---|
| `in` | `values`, `include_null?` | The member equals one of `values`. `NULL`, the "(blank)" member, is selected only with `include_null: true` |
| `not_in` | `values`, `exclude_null?` | The member equals none of `values`. `NULL`s are **kept** unless `exclude_null: true` (#238 rule 6); the engine never emits a bare SQL `NOT IN` |
| `range` | `gte?` or `gt?`, `lte?` or `lt?` | At least one bound, and at most one on each side |
| `is_null`, `is_not_null` | — | |
| `relative_date` | `unit`, `offset`, `count`, `to_date?` | `count` periods of `unit`, placed `offset` periods from the current one: whole periods unless `to_date: true`. Binding fixes the exact window and the sign convention of `offset` (P2-05b). Resolved when bound, against the query instant supplied by the caller and never the host clock, in the member's zone, else the model's `default_time_zone`; without either it is refused |
| `in_segment` | `segment` | The member is in the model's segment (S10) |
| `descendants_of` | `hierarchy`, `nodes` | A parent-child node selects all its descendants through the declared closure. Selecting a parent of a level hierarchy is an `in` on the parent level, which also includes children added later |

`values` and `nodes` hold 1–10,000 values. Duplicates are allowed, and removed when bound.

## Metric filters (#238 rule 5)

A metric filter is `{scope, metric, dimension, grain?, condition}`.
- It keeps the members of `dimension` whose `metric`, computed at `grain`, meets the `condition`. `grain` defaults to `[dimension]`.
- `condition` is either a comparison, `{op: gt | ge | lt | le | eq | ne, value}`, or a rank, `{op: top | bottom, n}`.
- It is evaluated in the context of every other filter, never of itself, and always under security.
- Its result is a member set, applied as a semi-join.
- Two metric filters that depend on each other on the same dimension and scope are refused when bound (`FILTER_CYCLE`).

## Values

A value is `{type, value}`. Every `value` except a boolean's is text:

| `type` | `value` |
|---|---|
| `text` | any text |
| `integer` | `-?(0\|[1-9][0-9]*)` |
| `decimal` | `-?(0\|[1-9][0-9]*)(\.[0-9]+)?`: exact. `1.50` and `1.5` are equal once bound, and the scale is the member's |
| `boolean` | `true` or `false` |
| `date` | `YYYY-MM-DD` |
| `timestamp` | RFC 3339 **with** an offset (`Z` or `±HH:MM`), up to nine fractional digits |
| `local_timestamp` | the same **without** an offset, read in the member's civil zone |

## Names

- **A member** follows the product's expression language (`ossie-extensions/v1`): `dataset.field`, `metric` for a model metric, or `dataset.metric` for a dataset metric.
- **A name** (a role, context, calendar, segment, hierarchy, calculation item or parameter) is one identifier.
- **Identifiers** follow Ossie's rule (`expression_language.md` L86–L88). A regular identifier is a letter or `_` followed by letters, digits or `_`, compared case-insensitively. A double-quoted one holds any text, with `""` for a quote, compared exactly.
- An identifier holds at most 128 characters. The schema bounds the text, and the binder checks each identifier.

## Text bounds

The schema checks shapes only. Binding also refuses, under rule `schema`:
- a date or timestamp that does not exist (`2026-13-45`, an hour of 25, an offset beyond ±14:00);
- a text that only a lenient pattern engine accepts, such as one ending in a line break.

It checks every identifier with Ossie's own rule, of which the schema's character class is only a bound. An identifier outside that class, such as one with a non-Latin letter beyond the Basic Multilingual Plane, is written quoted.

## Counts

`n`, `limit`, `page.size` and a relative date's `count` are positive counts, as text. `offset` is a signed integer, as text.

## When bound

The schema decides only the shape. Binding (P2-05b) then refuses, in this order:
- a repeated key or text that is not strict JSON: `QUERY_INVALID`, rule `syntax`;
- a document this schema rejects: `QUERY_INVALID`, rule `schema`;
- a member that does not exist, or a role that names no relationship: `MEMBER_NOT_FOUND`;
- a member of the wrong kind for its slot: `QUERY_INVALID`, rule `kind`;
- a relative date with no zone: `QUERY_INVALID`, rule `zone`;
- a parameter outside its type or `allowed`: `PARAMETER_INVALID`;
- `as_known_at` on a dataset without history: `AS_KNOWN_AT_UNSUPPORTED`;
- dependent metric filters: `FILTER_CYCLE`;
- a page above the cap: `QUERY_BUDGET_EXCEEDED`.

It then builds the canonical form and the query fingerprint (#283 §2).

## Compatibility

- **Compatible:** a new optional member, a new predicate or condition variant, a new value type, or a new enumerated value. A v1 consumer refuses what it does not know, so a producer sends one only to a consumer that declares it.
- **Incompatible:** removing or renaming a member, variant or value, changing a meaning or a default, or making an optional member required. These need `v2`, an ADR and a transition period.
