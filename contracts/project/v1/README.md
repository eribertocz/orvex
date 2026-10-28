# `project` contract v1

**Producer:** `server` (`orvex.server`, the project loader; the Builder writes the files by hand). **Consumers:** `server`; through the server, `transform` (sources, from P1-06) and `store` (retention, P1-14). Other modules add their own sections by extending this contract compatibly.

A project folder holds plain YAML files under version control (law 8). This contract defines two of them, parsed from YAML into the JSON data model, and the errors of loading them.

## Files (`schema.json`)

| Message | File | Content |
|---|---|---|
| `ProjectFile` | `project.yaml` (required) | `version: 1`; `name` (1–100 characters); `time_zone`, an IANA name, required (no implicit zone); `locale` `es` or `en` (default `es`); `retention.snapshots` `<n>d`, at least `1d` (default `90d`, decision #70); optional `lineage` (`url`, `https://`, or `http://` only to `localhost`/`127.0.0.1`; optional `api_key`, a `${env:NAME}` reference sent as a bearer token) where OpenLineage events are also sent (P1-18) |
| `ConnectionsFile` | `connections.yaml` (optional; absent = no connections) | `version: 1`; `connections`, a map from a name (`^[a-z][a-z0-9_]{0,62}$`, not `lake`) to one connection |
| `ProjectError` | — | `code`, `message`, `details` (`file`, `path` as a JSON pointer into the file, `line`, `connection`, `variable`) |

Connection types (`type`):

| Type | Fields |
|---|---|
| `parquet`, `csv` | `path` (glob allowed) |
| `duckdb`, `sqlite` | `path`, `read_only` |
| `postgres` | `host`, `port`, `database`, `user`, `password`, `read_only`, optional `schema` |
| `clickhouse` | `host`, `port`, `database`, `user`, `password`, `read_only`, optional `secure` (default `true`) |

Relative `path` values are resolved against the project folder by the loader.

## Secrets

`password` (and `lineage.api_key`) accepts **only** a reference `${env:NAME}` (`NAME` = `^[A-Z_][A-Z0-9_]*$`); a literal value is a schema error. The loader keeps the reference; the value is read from the environment only when a connection is used, and never appears in loaded objects, logs or errors. An unset variable is `PROJECT_SECRET_MISSING`, naming the variable.

## Read-only sources

`read_only` is required for database connections. `false` validates against the schema but the loader refuses it with `SOURCE_NOT_READ_ONLY`: every database source is read-only in v1.

## Checks beyond the schema (loader)

| Code | When |
|---|---|
| `PROJECT_NOT_FOUND` | the folder or its `project.yaml` does not exist, or a file is not a regular file or cannot be read |
| `PROJECT_YAML_INVALID` | not UTF-8 text, not valid YAML, more than one document, a duplicate key, an anchor, alias or merge key (`&`, `*`, `<<`), a file over 1 MiB, or values nested more than 32 levels, with its line |
| `PROJECT_VERSION_UNSUPPORTED` | `version` is an integer other than 1 |
| `PROJECT_SCHEMA_INVALID` | the file does not match its message, `time_zone` is not a zone of the bundled IANA database (system-only names such as `localtime` and the placeholder `Factory` are refused), or a connection name appears twice |
| `SOURCE_NOT_READ_ONLY` | a database connection declares `read_only: false` |
| `PROJECT_SECRET_MISSING` | a referenced environment variable is not set when the secret is resolved |

## Compatibility

New optional fields, new connection types and new error codes are compatible changes. Renaming or removing a field, making an optional field required, or changing a default is breaking (new `v2`).
