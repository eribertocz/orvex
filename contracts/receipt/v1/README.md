# `receipt` contract v1

**Defined and produced by** `semantic` (`contracts.toml`). **Read by** `server` (HTTP, P2-27), `mcp` and `web`. **Consumes** `semantic-query/v1` (its question) and `semantic-result/v1` (whose canonical form the result MAC covers, and whose envelope references the receipt) (#303).

Every result of the semantic engine has one receipt. It records:
- what was asked;
- under which model semantics and permissions;
- which plan and evidence answered it;
- which data versions were read;
- whether the result can be reproduced.

The server keeps the **record**. This contract defines what leaves the server: a **view** of it. No view reveals what its reader may not know.

## Views (Owner decision of 2026-11-02)

| View | Who gets it | Holds |
|---|---|---|
| `summary` | every caller, and any viewer that is not a well-formed viewer | the identity and seal; the question, restricted (below); whether policies restricted the result, and the policies the viewer may know of; freshness per dataset the viewer sees; reproducibility; cache hit or miss |
| `builder` | a caller with builder permission for the project | the summary, plus the model, the plan obligations, the evidence, the data versions, the execution (engine and adapter versions, timings, figures) and the result MAC |

**Being a builder does not grant unrestricted access.** Every item of the builder view also passes the viewer's own visibility:
- a member the viewer cannot see is left out, filters included;
- a policy the viewer may not know of is not listed, nor the data version read for it;
- an exemption (`applied: exempt`) is shown only to a viewer who may know that policy.

**Views are built by allow-list.** Each item is chosen from the record and checked against the viewer, one by one. A view is never a full record with fields hidden afterwards. A field added to the record reaches no view unless the allow-list names it.

**No view ever holds:**
- SQL, predicates, bound parameter values, mapping tables or their values, or value-set sizes;
- a hidden member;
- a masked member's type;
- estimates or actual figures for a result restricted by a row policy, because they would reveal a table's size (#238 rule 12).

### The question
The question is the canonical query (`semantic-query` v1), restricted to what the viewer may know:
- an entry that names a member the viewer cannot see is left out;
- a filter on a visible member whose values the viewer may not see keeps its member and scope and becomes `withheld: true`, with no predicate;
- the page cursor is never shown, only the page size;
- `withheld: true` at the top says that something was left out, never what.

## Identity and authentication

| Member | Is | Proves |
|---|---|---|
| `receipt_id` | the fingerprint (RFC 8785, SHA-256) of the record | identity only: anyone can recompute a hash |
| `seal` | `{key_id, mac}`: HMAC-SHA-256 of the record's canonical bytes under an instance key | authentication: a view with a missing or wrong seal is not genuine |
| `result` (builder) | `{key_id, mac}`: HMAC-SHA-256 of the result's canonical form (`semantic-result` v1, without its `receipt` reference) under an instance key | that a replay gave the same result, without storing values. A plain hash is not used: over a small domain, anyone holding it could guess the values (#236). |

A view is checked on the server against the record it was built from (`verify_receipt`). A view edited in any way, its `receipt_id` recomputed or not, is refused. The keys are held by the server and never appear in a view.

## Reproducibility
- **`class: exact`** means every input needed to replay is pinned and retained: every snapshot, the model, a stable reader, and the engine and adapter versions.
  - It claims that a replay is possible, not that one succeeded.
  - A retained snapshot alone never makes a receipt `exact`.
- **`class: explainable`** comes with its `reasons`:
  - `snapshot_expired`;
  - `live_read`;
  - `experimental_reader` (ADR-0030 §6);
  - `engine_unavailable`.
- **`replay`** is `not_checked` on a new receipt. Only a replay on the server sets `verified` (the same result MAC) or `failed`. That makes a new record with a new `receipt_id`.
- The class is derived from the record, never given. `retained` is true when the receipt is built; it can change later, and the view states the class it had then.

## Execution identity
`execution_fingerprint` (builder) is the execution identity of ADR-0030 §6. It covers the compiled model, the query fingerprint, the permission fingerprint, every data version read, and the engine and adapter versions. It never covers the cache origin, timings or the result. The result cache (P2-12) is keyed by it.

## Data versions
- **Managed lake:** the snapshot id.
- **Existing DuckLake:** the project, connection, snapshot id and commit time, never the id alone (ADR-0027 §5).
- **Live engine** (after v1.0.0): the read instant. It is never retained.

A data version read for a policy is named by the policy, never by its mapping table.

## Compatibility
- **Compatible:**
  - a new optional member;
  - a new obligation rule or evidence kind;
  - a new `reasons` value (a reader that does not know one treats the class as `explainable`).
- **Incompatible:**
  - changing the execution identity;
  - changing the `receipt_id` or seal input, or the result MAC input;
  - letting a view hold anything it may not hold now;
  - making an optional member required.

  These need `v2`, an ADR and a transition period.
