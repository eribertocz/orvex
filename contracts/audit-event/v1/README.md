# `audit-event` contract v1

**Defined and produced by** `access` (`contracts.toml`). **Recorded by** `access` for every module, through one writer (`record`, #437). Law 9.

One event is one security-relevant fact: a sign-in, a change to who may do what, a page view, a query, an export, an MCP call, a policy that left a caller without rows, a refusal, a certification, and the log's own maintenance.

## The document

Every object is closed.

| Member | Holds |
|---|---|
| `version` | `1` |
| `id` | the event's id: opaque, random |
| `at` | when it happened: UTC, with microseconds, `Z` |
| `actor` | the user id, or `system` for the product itself |
| `instance` | the instance's id |
| `project` | the project's id, when the event belongs to one |
| `kind` | what happened, from the list below |
| `outcome` | `allowed` or `refused` |
| `details` | a closed object per `kind` |

**Kinds.**
- Sign-in: `auth.login`, `auth.login_failed`, `auth.logout`, `auth.session_expired`.
- Users: `user.created`, `user.deactivated`, `user.reactivated`, `user.administrator_granted`, `user.administrator_removed`, `user.password_changed` (by the user), `user.password_reset` (by an administrator, who receives a temporary password), `user.oidc_linked` and `user.oidc_unlinked` (an administrator's links of a provider identity, #439).
- Memberships: `membership.added`, `membership.changed`, `membership.removed`.
- Groups: `group.created`, `group.deleted`, `group.member_added`, `group.member_removed`.
- Data: `page.view`, `query`, `export`, `mcp.call`.
- Policies and refusals: `policy.attribute_missing`, `policy.mapping_missing`, `access.refused`.
- Model: `model.certified`.
- Attributes (#440): `attribute.set` (the user, the attribute, its type, the SHA-256 of its canonical value, and the value itself when it is at most 1,024 characters) and `attribute.removed`, by a Builder of the project.
- The instance: `instance.settings_changed`, which names the sign-in settings that changed, never their values; `instance.oidc_mapping_changed`, when a provider's group mapping differs from the one last in force (#439).
- An OIDC sign-in is `auth.login` or `auth.login_failed` with `method: oidc`. A failure's `reason` is one of `STATE`, `ERROR_RESPONSE`, `ISSUER`, `TOKEN_EXCHANGE`, `SIGNATURE`, `CLAIMS`, `NONCE`, `UNLINKED`, `DEACTIVATED`, `NETWORK` and `REFRESH`; a group the mapping sets is `group.member_added` or `group.member_removed` by `system`.
- The log itself: `audit.retention`, `audit.key_rotated`.

Adding a kind is a compatible change. Removing one, or changing its meaning, needs `v2`.

**`details` holds identifiers, codes, counts and fingerprints only.** Each `kind` has its own schema with no other member. So a field that could carry a password, a token, a secret, free text or a result value cannot exist. A query is recorded by its receipt's fingerprint, never by its rows.

## The log

| Column | Holds |
|---|---|
| `seq` | the event's place in the one total order: gapless, from 1 |
| `hash` | SHA-256 of the previous event's `hash` (32 zero bytes before the first) followed by the event's canonical JSON |

**Canonical JSON** is RFC 8785 (JCS). An event holds only strings, integers within ±(2^53−1), booleans, objects with ASCII member names, and arrays. For those, JCS is the standard library's `json.dumps` with sorted keys, no spaces and no ASCII escaping. The access module tests that equivalence against the vendored RFC 8785 test data (`vendor/json-canonicalization`).

**Signatures.** All signatures use one instance key: Ed25519, kept in a file only its owner can read, never in the database.
- **The head**, `(seq, hash)` of the newest event, is signed by every write.
- **A checkpoint**, `(seq, hash, at)`, is signed every 1,000 events and at least daily while events are written. Each checkpoint is also appended to `audit-anchors.log`, a plain file outside the store, for the administrator to ship elsewhere: for example, to a write-once store.
- **A key rotation** records the new public key signed by the old one, and the old one signed by the new one. Verification trusts the current key and walks back through those signatures.

## What is detected, and what is not

**Detected by `verify`**, by anyone who can write the database but cannot read the signing key:
- a change, reordering or insertion of an event, or a change of a checkpoint, anywhere in the log, including a whole chain rewritten with consistent hashes, because no new signature can be made;
- a removal of events from the start of the log, unless retention made it: the log must start right after the newest `audit.retention` event's range, at a signed checkpoint;
- a removal from the middle, which leaves a gap.

**Detected only with an anchor shipped outside:** any change made before the newest anchor that was shipped, even by someone who also holds the key. `verify` compares the anchors it is given with the checkpoints.

**Not detected:**
- A **rollback** made after the last anchor that was shipped outside, by anyone who can write the database: the newest events removed, and the head and the checkpoints put back as they were signed then. The signed values are genuine, only older, and the database holds nothing newer to compare them with. Only an anchor shipped after that point shows it.
- A change by someone who holds both database access and the signing key, made after the last anchor that was shipped outside.

Shipping the anchor file often is what shortens both.

**What the database itself enforces.** This is defense in depth, not the guarantee:
- On SQLite, triggers refuse changes, and anyone who can write the file can drop them.
- On PostgreSQL, the store's role holds no `UPDATE`, `DELETE` or `TRUNCATE` grant on the log's tables. But it owns them, so it can grant itself those rights again. Only a separate owning role would close that, and the guarantee above does not depend on it.

## Retention

- The administrator sets `audit.retention_days`: 730 by default, at least 30 (`AUDIT_RETENTION_INVALID` below that).
- Retention first verifies the whole log, with the local anchor file, in the same transaction, holding the store's write lock: writers wait meanwhile (about a minute for ten million events), so run it at a quiet hour. A log that does not verify is not trimmed: the ages it would trust are then those of the signed, chained documents, which no database writer can change.
- Events older than that are removed, but only up to a signed checkpoint, which stays. The remaining chain therefore verifies from that checkpoint's hash.
- Retention with old events but no checkpoint to stop at is refused (`AUDIT_RETENTION_INVALID`).
- Each removal is itself an event, `audit.retention`, with the range removed.
- On PostgreSQL the module's role holds no `UPDATE` or `DELETE` grant on the events. Retention runs through one dedicated function.

## Compatibility
- `v1` members and kinds are stable.
- New kinds and new optional members are compatible changes.
