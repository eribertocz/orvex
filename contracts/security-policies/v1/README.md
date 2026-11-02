# `security-policies` contract v1

**Defined and produced by** `semantic` (`contracts.toml`). **Read by** `server` (it loads `security.yaml` and supplies the project's groups and managed-lake tables) and, later, the Builder (#304).

A project's `security.yaml` says which rows and which members each caller may see. The engine reads it into an immutable policy model, bound to the compiled model, before any query runs. Only the policies in this file exist (`security.md` §3.3). This issue defines and validates them; placing them in plans is P2-21, P3-07 and P3-08.

## The file

YAML, read strictly as JSON:
- one document;
- no anchors, aliases or tags;
- no duplicate or non-string keys;
- at most 1 MiB and 32 levels deep.

A quoted scalar is text. A plain `true` or `false` is a boolean, a plain decimal integer an integer, and `null` or `~` null. Anything else is text, so `yes`, `on`, `010` and `1e3` stay text.

| Member | Holds |
|---|---|
| `version` | `1` |
| `row_policies` | which rows of a dataset a caller sees |
| `column_policies` | whether a field or metric is `visible`, `masked` or `hidden` for a caller |
| `aggregate_thresholds` | minimum group sizes (#238 rule 10), representable now, executed later |
| `limits.policy_values` | the cap on values bound for another engine (ADR-0027 §6), default 1,000 |

### Row policies
- **Members:** `name` (unique; `^[a-z][a-z0-9_]{0,62}$`), `dataset`, `kind` and:
  - `exempt_groups`: groups exempt from this policy only;
  - `orphans`: `hidden` (default) or `visible`;
  - `roles`: the role paths (relationship names) the policy applies to; all of them by default.
- **`kind: predicate`** takes a `predicate` in the grammar below.
- **`kind: hierarchy`** takes:
  - `closure`: a managed-lake table with `ancestor` and `descendant` columns, each node paired with itself included;
  - `node`: the caller's attribute holding their node;
  - `field`: the dataset field holding a row's node.

  It is representable now and executed after P3-07 (#238 rule 8).
- **Deny-lists** (`NOT IN`, `NOT EXISTS`, or either under `NOT`) over a mapping table must declare `registration: {table, key, attribute?}`. It names the managed-lake table that lists the registered callers: `key` holds the caller's `attribute`, `user_id` by default.
  - The registration adds no value to the policy's value set.
  - A caller absent from it sees zero rows, with the notice `POLICY_MAPPING_MISSING` (ADR-0027 §6; Owner decision of 2026-11-02, #123).

### The predicate grammar
A closed subset of SQL, refused outside it (`POLICY_INVALID`, rule `predicate`). It may read:
- the fields of the policy's dataset;
- `$attribute` parameters from the security context, always bound and never text;
- string and integer literals, and `TRUE` and `FALSE`.

It may combine them with:
- comparisons (`=`, `<>`, `!=`, `<`, `<=`, `>`, `>=`) and `IS [NOT] NULL`;
- `AND`, `OR`, `NOT` and parentheses;
- `field [NOT] IN ($list_attribute)`;
- `field [NOT] IN (SELECT column FROM schema.table WHERE condition AND …)` and `[NOT] EXISTS (SELECT 1 FROM schema.table WHERE condition AND …)`. A condition is one column of that table equal to a field (qualified by the dataset's name inside the subquery), an attribute or a literal.

It refuses everything else:
- function calls and arithmetic;
- a selected column qualified by another table, and a mapping table named like the policy's dataset (its qualifier would be ambiguous);
- nesting deeper than 32 levels;
- concatenation (`||`), comments and semicolons;
- a second table;
- a non-equality inside a subquery;
- an attribute written inside a string.

### Mapping tables
Mapping, registration and closure tables live in the managed lake. A subquery on any other table is refused with `POLICY_NOT_SUPPORTED_ON_ENGINE` (`reason: subquery_outside_lake`): that table's writer would otherwise decide who sees which rows (ADR-0027 §6).

### Column policies
- `member`: a field, a dataset metric or a model metric.
- `default`: `visible`, `masked` (with `mask`, 1–64 characters) or `hidden`.
- `visible_to_groups`: the groups that see it visible.
- `visible_when` (#238 rule 9): a row predicate. Where it is false the value is masked, so it requires `mask`, and its `default` is `visible` or `masked`: a hidden member stays hidden. It is representable now and executed in P3. Until then, its member is listed as masked and its dataset is unusable.
- A metric is `visible` or `hidden`, never masked (`security.md` §3.3; rule `masked_metric`).
- Exactly one column policy per member (`DUPLICATE_COLUMN_POLICY`).

## Combination rules (normative for P2-21 and P3; #238 rule 2)
- Row policies on a dataset, local and propagated, combine with **AND**: each one can only restrict.
- An OR is written inside one policy, never implied.
- `exempt_groups` exempts from that policy only.
- A caller in several groups needs one matching group (in `exempt_groups` or `visible_to_groups`).
- A missing attribute makes its policy, and so the result, give zero rows, with the generic notice `POLICY_ATTRIBUTE_MISSING`.
- **No implicit exemption** (Owner decisions of 2026-11-01 and 2026-11-02):
  - no role exempts anyone, administrators and editors included;
  - an exemption exists only where a policy names a group;
  - it is recorded in the receipt and revealed only to authorized viewers (`receipt` v1);
  - removing it takes effect with the next policy model.

  Validation refuses an unknown group.

## What the policy model computes
- **Inherited protection, against inference.** A member gets the strictest protection of every protected member it reads, by any path:
  - its expression and lineage (P2-04b);
  - a metric's context steps (`keep`, `remove` targets);
  - a window's partition and order (a bare dimension name counts every field of that name);
  - and, for relationship keys, the paired column of the related dataset.

  A hidden source makes it hidden. A masked source makes a metric hidden and a field masked. A model segment whose dimension, metric or condition reads a protected member, and a `member` or `metric` parameter whose default or allowed values name one, are refused (`POLICY_INVALID`, rule `inference`).
- **Propagation** (#238 rule 1). For each row policy, every dataset it reaches, by every simple path:
  - many-to-one relationships from the "one" side to the "many" side, in chains of any length;
  - one-to-one relationships in both directions;
  - each role path, unless the policy names its roles;
  - bridges, as `bridge_allocation`;
  - cycles, a self-relationship included: reported once and never looped (the relationship walked straight back, as in a one-to-one, is not a new path).

  `roles` may name only role relationships (two or more between one pair of datasets) on this policy's paths: any other name is refused (`reference`), so a typo never silently drops a path.

  Declared and verified cardinality count alike. A policy on a fact reaches no dimension. Over 256 paths for one policy is refused (rule `propagation`).
- **Non-executable policies fail closed.** These datasets are unusable until their execution lands:
  - one with a hierarchy policy, or reached by one;
  - one with a `visible_when` member;
  - one covered by an aggregate threshold.

  A query on them is refused with `UNSUPPORTED_OPERATION` (`operation`: `hierarchy_policy`, `cell_policy` or `min_group_size`). It is never served without the policy.
- **The fingerprint** of the policy model (`security_policies`, RFC 8785) ignores the order of unordered collections and changes with any meaning. It is an input of the permission fingerprint (P2-09).

## Errors
- `POLICY_INVALID` carries `file` (public) and up to 50 problems (builder: a pointer, a line and a rule). `DUPLICATE_COLUMN_POLICY` carries the member, a builder detail.
- No error ever carries a predicate, a value or a mapping table to an audience other than the builder.

## Compatibility
- **Compatible:**
  - a new optional member;
  - a new policy kind that validates and fails closed until it runs;
  - a new grammar construct that only restricts.
- **Incompatible:**
  - changing a combination rule, the grammar's meaning or the fail-closed rules;
  - making an optional member required.

  These need `v2`, an ADR and a transition period.
