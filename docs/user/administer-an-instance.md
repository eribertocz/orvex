# Administer a server instance

A server instance keeps its users, their project roles and groups, and its administrators in
PostgreSQL. Local mode needs none of this: it has one implicit user, who is administrator and
Builder.

## Where the users are kept

| Mode | Store |
|---|---|
| Local | `access.sqlite` in the instance directory (`~/.orvex`, or `ORVEX_HOME`) |
| Server | PostgreSQL, always |

Server mode reads two environment variables, and refuses to start when either is missing
(`ACCESS_STORE_NOT_CONFIGURED`). It never falls back to SQLite.

| Variable | Holds |
|---|---|
| `ORVEX_ACCESS_POSTGRES` | the connection: `host=… port=… dbname=… user=…`, and optionally `sslmode=…` and `sslrootcert=…` |
| `ORVEX_ACCESS_POSTGRES_PASSWORD` | the password of that user |

- **TLS.** A PostgreSQL server on another machine is reached only with `sslmode=verify-full`, which checks the server's certificate and its host name. Give `sslrootcert=<file>` when your certificate authority is not in the system's trust store. Without TLS (`sslmode=disable`, or no `sslmode`), only `localhost`, `127.0.0.1`, `::1` or a Unix socket directory is accepted. Any other setting is refused, never weakened.
- **Its own schema and role.** The users live in the `access` schema, which the connected role creates and owns, with no privilege for anyone else. Give that role to nothing else. A schema `access` owned by another role is refused (`ACCESS_STORE_UNSAFE`).

## Create the first administrator

On the server machine, with the variables above set:

```bash
orvex admin create --user-name ana --display-name "Ana Pérez"
```

- The first run writes the instance's **recovery file**, `access-recovery.key` in the instance directory, readable only by you. Keep a copy offline and remove it from the server if your policy requires.
- The command is refused once an active administrator exists (`ACCESS_FORBIDDEN`).

## Recover when no administrator can sign in

```bash
orvex admin create --user-name rescue --display-name "Rescue" --recover \
  --recovery-file /path/to/access-recovery.key
```

`--recover` creates another administrator even when administrators exist. It needs the
recovery file written at the first run. The file must be a regular file (not a link), owned by
you, and readable by nobody else; anything else is refused, without saying which check failed.

Every use of `admin create`, done or refused, is recorded. Until the audit log arrives, the
record is a line on standard error.

## The audit log

Every sign-in, every change to who may do what, every refusal, and the log's own maintenance are recorded in the access store's audit log (`contracts/audit-event/v1`). Nothing in it can be edited. An event holds identifiers, codes and fingerprints, never a password, a token or a result value.

**Tampering is detectable.** Each event is chained to the one before it with SHA-256. The chain is signed with the instance's **audit signing key**, an Ed25519 key that `admin create` writes the first time, readable only by you.

| Setting | Variable | Default |
|---|---|---|
| Signing key | `ORVEX_AUDIT_SIGNING_KEY` | `audit-signing.key` in the instance directory |
| Anchor file | `ORVEX_AUDIT_ANCHORS` | `audit-anchors.log` in the instance directory |
| Retention | `ORVEX_AUDIT_RETENTION_DAYS` | 730 (at least 30) |

- **Keep the key away from the database server.** In server mode, put it on a disk the PostgreSQL server cannot read. Anyone who can write the database *and* read the key could rewrite the log.
- **Ship the anchor file elsewhere, often.** Every 1,000 events, and at least daily while events are written, a signed checkpoint is appended to the anchor file. A copy kept outside the server (a write-once bucket, another machine) proves the log up to that copy, even against someone who holds the key.

```bash
orvex admin audit verify                              # the chain and every signature
orvex admin audit verify --anchors /backup/anchors.log  # also against a copy shipped outside
orvex admin audit retain                              # remove events older than the retention
orvex admin audit rotate-key                          # a new key, signed by the old one
```

`verify` names the first event or checkpoint that does not hold (`AUDIT_CHAIN_BROKEN`, `AUDIT_CHECKPOINT_INVALID`). `retain` removes old events only up to a signed checkpoint, which stays, so what remains still verifies. It verifies the whole log first and holds the store's write lock meanwhile, so run it at a quiet hour. It refuses when no checkpoint can be kept (`AUDIT_RETENTION_INVALID`).

If `rotate-key` stops between recording the rotation and replacing the file, the new key is left as `audit-signing.key.new`. Until you move it into place, the log refuses to write with the old one. Restart a running server after a rotation: it keeps the key it loaded, and refuses to write with it once it has been replaced.
