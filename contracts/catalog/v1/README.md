# `catalog` contract v1

**Defined and produced by** `semantic` (`contracts.toml`). **Read by** `server` (HTTP `/catalog`, P2-27), `mcp` and `web` (#309).

A catalog tells one caller what they may query: datasets, fields, metrics and relationships, each with its type, unit and description. Every caller gets their own catalog, built from their security context and the project's policies (`security-policies` v1). It holds no data.

## The document

| Member | Holds |
|---|---|
| `kind`, `version` | `catalog`, `1` |
| `model_fingerprint` | the compiled model's identity (ADR-0030 §6) |
| `catalog_fingerprint` | the fingerprint (RFC 8785) of this catalog's contents. Two callers with the same effective visibility get the same one; any difference in visibility changes it |
| `datasets` | the datasets the caller may use, in the model's order |
| `metrics` | the model metrics the caller may use |
| `relationships` | the relationships between listed datasets whose keys the caller may see |

A **dataset** has `name`, `description?`, `grain?`, `available: true`, `fields` and `metrics`.

A **field or metric** has:
- `name` and `description?`;
- `logical`: the engine-independent type, as in `semantic-result` v1, plus `Time`; a decimal whose precision is still undecided lists none;
- `semantic_type?`;
- `unit?` or `currency?`, never both;
- `exactness`: `exact`, `approximate` or `unknown`;
- `nullable`.

A **masked field** is only `{name, masked: true, encoding: "VARCHAR"}`.

## Who sees what
- **Hidden is absent.** These are left out, and their absence cannot be told from their not existing:
  - a member whose effective protection is `hidden` for the caller's groups, inherited protection included (`security-policies` v1);
  - a dataset with no visible member;
  - a relationship with a hidden end, or with a hidden or masked key.

  A relationship carries no `role` today, because the model payload declares none.

  `model_fingerprint` identifies the model and is the same for every caller; it says nothing about which members are hidden.
- **Masked shows nothing of itself.** No type, unit, semantic type, exactness, nullity or description.
- **Unavailable datasets** (Owner decision of 2026-11-02). A dataset that a policy not yet executable (hierarchy, a minimum group size) makes unusable is listed as `{name, available: false, reason: "not_available_yet"}`, with no members. It is listed only when its existence is authorized, that is, when the caller would see at least one of its members. Otherwise it is absent. A model metric that reads an unavailable dataset is absent.
- **Descriptions** (Owner decision of 2026-11-02). A description is the modeler's text.
  - The compiler flags it when it names a protected member, by its name alone or as `dataset.member`, as a whole word in any case.
  - A flagged description is omitted for every caller without builder permission.
  - Builders see it with `description_flagged: true` until the modeler fixes it.
  - The check is lexical: a paraphrase that names no protected member is not caught. The modeler owns the text.
- **Roles never widen visibility.** Visibility follows the caller's groups alone. An administrator or editor sees what their groups allow, and builder permission only shows flagged descriptions (#238).
- **The caller's context** is a `security-context` v1 document built for this request (P2-09). A malformed one is refused, never read as a caller with other permissions. A catalog built for one context is never served under another: a cached catalog is reused only when `may_reuse` confirms the caller's current permission fingerprint, and the server composition (P2-27) applies that check.

## Ossie export (ADR-0030 §5)
- **`with_extensions`:** the model document's own bytes, lossless.
- **`standard_only`:** the document without its `ORVEX` payloads, plus a loss report. The report lists each dropped declaration and what an Ossie-only reader would then do differently, for example "may join without knowing the relationship's cardinality, and fan out".
- **Only a builder exports**, and only the whole model (Owner decision of 2026-11-02). `security.yaml`, policies, mapping tables and data are never exported.
- A conversion to another registry entry is refused; today there is one entry.

## Compatibility
- **Compatible:** a new optional member; a new `reason` for an unavailable dataset.
- **Incompatible:**
  - showing anything a hidden or masked member now hides;
  - listing an unavailable dataset to a caller who now does not see it;
  - changing the fingerprint's input;
  - making an optional member required.

  These need `v2`, an ADR and a transition period.
