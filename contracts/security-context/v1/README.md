# `security-context` contract v1

**Defined and produced by** `access` (`contracts.toml`). **Consumed by** `semantic`, which never reads users or sessions itself (#306).

A security context says who the caller is and what they may see, for one project. A new one is built for every request, from current state.

## The document

Every object is closed.

| Member | Holds |
|---|---|
| `kind`, `version` | `security_context`, `1` |
| `user` | the internal user id: opaque (`^[A-Za-z0-9_-]{1,128}$`), never an email or a display name (`security.md` §2) |
| `project` | the project id |
| `role` | `builder`, `author` or `viewer` (`security.md` §3.1) |
| `groups` | the caller's project groups, normalized (below) |
| `attributes` | name → typed value; names match `^[a-z][a-z0-9_]{0,62}$`, at most 64 |
| `permission_fingerprint` | below |

**Typed, exact attributes.** Each attribute is one of:
- `{"type": "text", "value": "…"}`: 1–1,024 characters;
- `{"type": "integer", "value": "…"}`: decimal text, never a float;
- `{"type": "boolean", "value": true}`: so `true` is never `"true"`;
- `{"type": "list", "values": […]}`: 1–1,000 texts, normalized.

**`$user_id` is the context's own user** (#440). The engine binds it from `user`. An attribute named `user_id` is redundant; one whose value or type differs from `user` makes the document invalid, and it is refused, never read as either user. Producers never write one: `access` reserves the name.

**A missing attribute is absent**, never `null` or empty, so it cannot match a policy by accident. A policy that needs it gives zero rows, with `POLICY_ATTRIBUTE_MISSING` (`security-policies` v1).

**A context never holds:**
- the instance-administrator flag, because administration grants no data access (Owner decision on #238);
- credentials, session ids or tokens;
- any data value beyond the caller's own attributes.

## Normalization, then validation (Owner decision of 2026-11-02)
The provider normalizes lists exactly once, before validation:
- groups and list attributes are NFC-normalized, deduplicated (forms that differ only by normalization count as one), and sorted by Unicode code point;
- nothing else is normalized: no case folding and no trimming, because those could merge two different groups.

The contract accepts only the normalized form. A document with an unsorted or duplicated list is refused, never repaired. The schema refuses duplicates, and the reader (`read_context`) refuses an order that is not the normalized one.

## The permission fingerprint
`sha256:` and the SHA-256 of `security-context/v1\n` followed by the byte form of one document. That document holds:
- `user`, `project`, `role`, `groups` and `attributes` (each attribute as its typed object);
- `policies`: the policy model's fingerprint (`security-policies` v1);
- `mapping_snapshots`: managed-lake table → snapshot, for every table the project's policies and registration checks read (mapping, registration and closure tables; #238 rule 8).

The byte form is unambiguous and language-neutral:
- a string is `s`, its UTF-8 length (8 bytes, big-endian), then its UTF-8 bytes;
- a boolean is `b` and one byte, `01` or `00`;
- a list is `l`, its length (8 bytes), then its items;
- a map is `m`, its size (8 bytes), then each key and value, keys ordered by their UTF-8 bytes.

`user` is always included. This is secure by default: two callers never share a cache entry, even with equal groups and attributes. Any change in user, project, role, groups, attributes, policies or mapping data changes the fingerprint.

## Current permissions before any reuse (Owner decision of 2026-11-02)
- A cached result or a page cursor is reused only after the caller's context has been built anew for the request, from current state.
- Reuse happens only when that fresh fingerprint equals the stored one (`may_reuse`). P2-06's `verify_cursor` receives a binding built from the fresh fingerprint (`current_fingerprint`), and P2-12's cache uses the same rule.
- A fingerprint carried by a request, a cursor or a cache entry is never trusted on its own.
- A document's `permission_fingerprint` cannot be recomputed from the document alone, because the policy fingerprint and the mapping snapshots are not in it. It is trusted only as it comes from the provider for the current request, and never to bind a cursor or a receipt after a round trip outside the server.

Every `pattern` matches the whole value: `$` never matches before a final newline.

## Compatibility
- **Compatible:** a new optional member; a new attribute type that existing readers refuse (fail closed) until they know it.
- **Incompatible:**
  - changing the byte form or the fingerprint's inputs;
  - the normalization;
  - the absent-not-null rule;
  - making an optional member required.

  These need `v2`, an ADR and a transition period.
