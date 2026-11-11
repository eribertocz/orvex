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
