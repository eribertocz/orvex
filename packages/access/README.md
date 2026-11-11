# `orvex-access` — `orvex.access`

Know who the caller is and what they may do: users, sessions, roles, audit.

Module pack: [`docs/modules/access.md`](../../docs/modules/access.md).

- `orvex.access.context`: the security context and the permission fingerprint.
- `orvex.access.directory`: users, project memberships, groups and administrators, with
  `admin create`'s bootstrap and recovery.
- `orvex.access.audit`: the audit log (`audit-event` v1), its hash chain, signed checkpoints,
  anchors, retention and key rotation.
- `orvex.access.storage`: the module's own store, SQLite (local mode) or PostgreSQL (server mode).

## Tests

The directory tests run on both stores. The PostgreSQL ones need `ACCESS_TEST_POSTGRES` (a
connection string without password) and `ACCESS_TEST_POSTGRES_PASSWORD`; without them they are
skipped, except in CI, where they are required.

```bash
pytest packages/access
```
