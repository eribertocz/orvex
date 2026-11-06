# `semantic-result` contract v1

**Defined and produced by** `semantic` (`contracts.toml`). **Read by** `server` (HTTP, P2-27), `mcp` and `web`. **Consumes** `store/v1`, whose transport types and `ResultJson` encoding it reuses; `semantic-query/v1`, whose bound query it answers; and `semantic-error/v1`, whose warnings and notices it carries (#293).

Every answer of the semantic engine has this one form, the same for the library, HTTP and MCP. It says:
- what each column is and how its values are encoded;
- which query and which model semantics produced it;
- every cell that has no ordinary value, and why;
- how to fetch the next page.

An exact value never becomes a float. A value, `NULL`, a mask and a cell state are never confused, and a masked or hidden member is never revealed.

## Envelope

Every member below is required unless marked optional. Every object is closed, and the only JSON numbers are small counts and indexes, within ±(2^53−1).

| Member | Holds |
|---|---|
| `kind`, `version` | `semantic_result`, `1` |
| `mode` | `aggregate` or `detail`, as in the query |
| `query_fingerprint` | the bound query's fingerprint (`semantic-query` v1, P2-05) |
| `model_fingerprint` | the compiled model's identity (P2-04, ADR-0030 §6) |
| `columns` | 1–512 column descriptors, in the query's presentation order |
| `rows` | the rows of this response, positional arrays of cells, in the plan's order (never reordered here) |
| `row_count` | the number of rows in this response |
| `cell_states` | `{row, column, state}` entries: the cells with no ordinary value (below) |
| `page` | `detail`: `{size, next?}`, where `next` is absent on the last page. `aggregate`: `{limit?, truncated}`, never a cursor |
| `others_row` | optional, `aggregate` only: the index of the recomputed "Others" row (S10 `top_n` with `others: true`) |
| `row_roles` | `aggregate` only, present exactly when the query asks for `totals` (P2-23): one `{role, level}` per row, below |
| `notices` | at most 64 `semantic-error/v1` messages of severity `warning` or `notice`, in the caller's audience view, so a notice never discloses more than an error would |
| `receipt` | optional: `{receipt_id, reproducibility}`, the reference to this result's receipt (`receipt` v1, P2-07). The canonical form never includes it, because the receipt's result MAC covers the canonical form |

**What the fingerprints mean.**
- They identify what was asked, and under which model semantics. They are not data provenance: two results with equal fingerprints may hold different data.
- The data version or snapshot, the engine and dialect-adapter versions, the permission fingerprint and the cache origin form the execution identity (ADR-0030 §6). They belong to the receipt (P2-07), whose reference the envelope gains as an optional member, a compatible change.

Fingerprints are `sha256:` and 64 lowercase hex digits.

## Columns

| Member | Holds |
|---|---|
| `name` | the output name, unique within the result |
| `member` | the bound query's canonical member: `{dataset?, name}`, with normalized identifiers and no `dataset` for a model metric. Only members the caller may see appear; a hidden member never reaches a result, because binding refuses it (`MEMBER_NOT_FOUND`) |
| `role` | `dimension`, `metric` or `field` (detail mode) |
| `context`, `apply`, `bucket` | echoed from the query, when present |
| `logical` | the engine-independent type, from P2-04's type system: a `family` and its parameters (below) |
| `encoding` | the `store/v1` transport type name, which fixes the Arrow type and the `ResultJson` encoding |
| `semantic_type`, `unit` or `currency`, `exactness` | from the compiled member. A column carries a `unit` or a `currency`, never both. `exactness` is `exact` or `approximate` |
| `nullable` | whether a cell may be `NULL` |
| `masked`, `mask` | `masked: true` and the declared `mask` text, only for a masked member |

### The type and its transport

`logical.family` is one of `String`, `Integer`, `Decimal`, `Float`, `Boolean`, `Date`, `DateTime`, `DateTimeTz` or `Opaque`. Its parameters:
- `Decimal`: `precision` (1–38) and `scale` (0–38), both required;
- `Integer`: `width` (8, 16, 32, 64 or 128) and `signed`, both required;
- `DateTime` and `DateTimeTz`: an optional `zone`, the civil zone or the named zone.

`store/v1` names its types after DuckDB's. That vocabulary is the transport every engine adapter maps onto, not a semantic type, and `encoding` is the only member that names it. These are the only allowed pairs:

| `logical` | `encoding` |
|---|---|
| `String` | `VARCHAR` |
| `Boolean` | `BOOLEAN` |
| `Integer` 8, 16, 32, 64, 128, signed | `TINYINT`, `SMALLINT`, `INTEGER`, `BIGINT`, `HUGEINT` |
| `Integer` 8, 16, 32, 64, 128, unsigned | `UTINYINT`, `USMALLINT`, `UINTEGER`, `UBIGINT`, `UHUGEINT` |
| `Decimal(p,s)` | `DECIMAL(p,s)`, the same `p` and `s` |
| `Float` | `FLOAT`, `DOUBLE` |
| `Date` | `DATE` |
| `DateTime` | `TIMESTAMP` |
| `DateTimeTz` | `TIMESTAMP WITH TIME ZONE` |
| `Opaque` | `UUID`, `BLOB`, `INTERVAL` |

The schema enforces each pair. The reference reader also checks that a `Decimal` descriptor's `precision` and `scale` equal its `DECIMAL(p,s)`.

**Time columns.** `store/v1` cannot transport Ossie `Time`, so no descriptor holds it. A query that would return such a member is refused upstream with `UNSUPPORTED_OPERATION` (`operation: time_column`), a capability error, never `OSSIE_INVALID`: the Ossie model stays valid (Owner decision of 2026-11-01).

### Masked columns (security §3.3)

A masked column has:
- `encoding` `VARCHAR`;
- `nullable` `false`;
- the `mask` text;
- no `logical`, `semantic_type`, `unit`, `currency` or `exactness`.

Every cell of a masked column is the mask, real `NULL`s included, so its nullity is hidden too (Owner decision of 2026-11-01). Its real values never reach the result: the plan selects the mask literal (P2-12).

## Cells

A cell is exactly one of four kinds, told apart by its descriptor and its JSON kind:

| Kind | JSON | When |
|---|---|---|
| value | the `ResultJson` encoding of the column's `encoding`: a string, number or boolean, never an object | an ordinary value |
| `NULL` | `null` | SQL `NULL`, in a column that is not masked. It is never zero, never an empty string, never a mask |
| mask | the column's `mask` text | every cell of a masked column |
| state | `{"state": "<name>"}` | a cell with no ordinary value |

A string equal to a mask is a value in a column that is not masked. A state is the only JSON object a cell may be, because `store/v1` refuses structured types.

`ResultJson` writes the exact types as strings:
- `BIGINT`, `UBIGINT`, `HUGEINT` and `UHUGEINT` as digit strings;
- `DECIMAL(p,s)` as plain text with exactly `s` fraction digits.

So a decimal cell is never a JSON number. `FLOAT` and `DOUBLE` are numbers, and `NaN` and the infinities are strings (`store/v1`).

## Cell states

**Known states in v1:**
- `undefined_at_total`: a window or comparison with no value at a total (S9);
- `others`: the dimension cells of the "Others" row (S10).

**One rule: the three representations state exactly the same states.**
- **`cell_states` is the source.** It is sorted by row, then column, with at most one entry per cell.
- **JSON:** the cell at `(row, column)` is `{"state": state}` if and only if `cell_states` holds that entry.
- **Arrow:** the cell at `(row, column)` is Arrow null when it is a state or a real `NULL`. The schema metadata carries the same `cell_states` list, byte-identical to the envelope's.

A result is invalid when any of these holds:
- an entry repeats a cell, or a cell has two states;
- an entry falls outside `row_count` or the columns, or sits on a masked column;
- a JSON state object has no entry, or an entry's JSON cell is not that state object;
- the Arrow cell under an entry is not null;
- the Arrow metadata differs from the envelope;
- an `others` entry sits off `others_row` or off a dimension column, or a dimension cell of `others_row` lacks it;
- `others_row` is set without `others` entries, or the reverse.

**Unknown states are safe.**
- A state is any name matching `^[a-z][a-z0-9_]{0,62}$`, so adding a state is compatible.
- A reader that meets a state it does not know treats the cell as *unavailable*:
  - never as `NULL`, zero, empty or a value;
  - never in a calculation, a total or a sort;
  - shown with a generic indicator.
- A producer emits only the states of the contract version it implements.

## Row roles (totals)

When the query asks for `totals` (`semantic-query` v1), `row_roles` has one entry per row, in row order:
- `detail`: the row keeps every `group_by` member, at least one; `level` is their number;
- `subtotal`: the row keeps the first `level` members, in presentation order, with `0 < level <` that number;
- `total`: the grand total, `level` 0, which is also the one row of a question with no `group_by`.

**A dimension a row does not keep is `null`**, so with totals every dimension column is `nullable`. That `null` is not a value: `row_roles` is what tells "every region" from a region that is `NULL`. A running total is `undefined_at_total` in every `subtotal` and `total` row.

A result is invalid when `row_roles` is present without `totals` in the query or in `detail` mode, when its length differs from `row_count`, when a role disagrees with its level, or when a dimension the row does not keep holds anything but `null`.

**Arrow:** the schema metadata key `semantic.row_roles` holds the RFC 8785 bytes of the envelope's `row_roles`.

## What the schema checks, and what the reader adds

A JSON Schema validates one object at a time, so it cannot relate a cell to the column at the same position. The schema checks:
- every member, closed objects and the mode variants;
- the `logical` ↔ `encoding` pairs and the masked-column shape;
- the cell kinds, state names, cursors and fingerprints;
- that a column carries a `unit` or a `currency`, never both.

The reference reader (`orvex.semantic`, P2-06b) and the producer (`build_result`, `encode_rows`) also check every rule that needs a cell's position:
- each value's JSON kind matches its column's encoding, so a decimal is never a number;
- every cell of a masked column is the mask, never `null`;
- column names are unique;
- `row_count` equals the rows;
- a `Decimal` descriptor's precision and scale equal its encoding's, with the scale at most the precision;
- the role follows the mode: `dimension` or `metric` in `aggregate`, `field` in `detail`;
- a masked column appears only in `detail` mode (a masked member cannot be grouped, `MEMBER_MASKED`);
- a column that is not masked states its `logical` type and its `exactness`, and a `Float` is `approximate`;
- every cell-state rule above.

Its fixtures, one per rule, are beside its tests (`packages/semantic/tests/result_fixtures`): the schema accepts each, and only the reader refuses it.

## The Arrow form

The same result as an Arrow table:
- each column has its `store/v1` Arrow type and keeps the `duckdb_type` field metadata key, which holds its `encoding`;
- a masked column is `utf8` and not nullable; every other field is nullable, because a state cell is Arrow null even in a column whose descriptor says `nullable: false` (the descriptor's `nullable` is about `NULL` values and travels in `semantic.nullable`);
- the field metadata carries the descriptor under neutral keys, `semantic.<member>` (`semantic.member`, `semantic.role`, `semantic.logical`, `semantic.unit`, …): strings as they are, anything else as RFC 8785 text;
- a state cell and a real `NULL` are both Arrow null; the schema metadata key `semantic.cell_states` holds the RFC 8785 bytes of the envelope's `cell_states`, and `semantic.others_row` the index of the "Others" row, when there is one. `cell_states` tells a state from a `NULL`.

## Page cursors (detail mode)

A cursor is opaque to clients: 1–1,024 characters from the URL-safe base64 alphabet (`A–Z a–z 0–9 _ -`). The contract fixes what a cursor means and how it is protected, not its layout.

**What it binds:**
- the `query_fingerprint` and the `model_fingerprint`;
- the caller's permission fingerprint;
- the data version (the snapshot on DuckLake routes, ADR-0027 §7);
- the page size;
- the position: the sort-key values of the last row returned, including the declared unique key that binding appends as the final tie-breaker. This is keyset pagination, never offsets.

**Confidential and authenticated.** A client can neither read nor alter what a cursor holds. A cursor is one of:
- **sealed**: encrypted with authenticated encryption (AEAD) under a server-held key, with a fresh nonce per cursor and the bindings as authenticated data;
- **server-side**: a random handle of at least 128 bits pointing to state kept by the server.

Either way, its bytes reveal no sort-key value, fingerprint, snapshot or page size, and two cursors for one position cannot be linked. The form, and any cryptographic dependency, is P2-12's choice under the dependency policy (ADR-0002, ADR-0022).

**Authorization on every request.**
- A cursor never grants access. Each page request is bound anew with the caller's current security context, as if it were the first page.
- The cursor is accepted only if that context's permission fingerprint equals the one it holds. After any change of roles, groups, attributes or policies, the caller restarts with the permissions they now have.

**Refusals:**
- A cursor that is malformed, forged, altered, unknown to the server, presented by another caller, or presented with another query, model, page size, permissions or data version is refused with `PAGE_CURSOR_INVALID`, `reason: invalid`. Nothing says which binding failed.
- When the cursor's snapshot is no longer available, it is refused with `reason: expired`, and the client restarts from the first page. `expired` is given only when every other check passes; otherwise the reason is `invalid`, so another caller never learns that a cursor is genuine.

**Data changes:**
- On a snapshot route, every page of one cursor reads the same snapshot, so rows are never repeated or skipped.
- Paging never continues silently on newer data.
- On live engines (after v1.0.0), rows may move between pages, and each page's receipt carries its read instant (ADR-0027 §5, P2-07).

A cursor holds nothing the page did not show: its sort keys are visible members. A masked member cannot be sorted (`MEMBER_MASKED`), and a hidden one cannot be referenced.

**The issue and verify contract** (`orvex.semantic.result.cursors`). A cursor form implements `issue(binding, state)` and `open(cursor, binding)`, which gives back the state of a genuine cursor issued under exactly that binding, and nothing otherwise. `verify_cursor(form, cursor, binding, snapshot)` holds the refusal rules for every form: the binding is computed anew for each request with the caller's current security context; `snapshot` says whether the cursor's data version is available, expired, or one the route never serves (another data version). A conformance suite (`packages/semantic/tests/test_page_cursors.py`) runs unchanged against a sealed and a server-side test form; the form that ships (P2-12) joins it.

## Canonical form

The wire form is the envelope with `rows` in the `ResultJson` encoding. The canonical form, used for hashing (P2-07 receipts), is RFC 8785 over the same envelope, with every non-string cell as text:
- integers as their decimal text;
- `FLOAT` and `DOUBLE` as their shortest round-trip text, keeping `-0.0`;
- `NaN` and the infinities, already strings.

This keeps it exact. The same table, descriptors and states give the same canonical bytes everywhere.

## Compatibility

- **Compatible:**
  - a new optional descriptor or envelope member, such as the receipt reference (P2-07, now present);
  - a new cell state (readers treat it as *unavailable*);
  - a new notice code.
- **Incompatible:**
  - changing a value encoding, the masking rule, the cursor bindings or the unknown-state rule;
  - making an optional member required;
  - removing or renaming a member.

  These need `v2`, an ADR and a transition period.
