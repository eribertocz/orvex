# `project` contract v1

**Producer:** `server` (`orvex.server`, the project loader; the Builder writes the files by hand). **Consumers:** `server`; through the server, `transform` (sources, from P1-06) and `store` (retention, P1-14). Other modules add their own sections by extending this contract compatibly.

A project folder holds plain YAML files under version control (law 8). This contract defines two of them, parsed from YAML into the JSON data model, and the errors of loading them.

## Files (`schema.json`)

| Message | File | Content |
|---|---|---|
| `ProjectFile` | `project.yaml` (required) | `version: 1`; `name` (1–100 characters); `time_zone`, an IANA name, required (no implicit zone); `locale` `es` or `en` (default `es`); `retention.snapshots` `<n>d`, at least `1d` (default `90d`, decision #70); optional `lineage` (`url`, `https://`, or `http://` only to `localhost`/`127.0.0.1`, never with a user or password; optional `api_key`, a `${env:NAME}` reference sent as a bearer token) where OpenLineage events are also sent (P1-18); optional `lake` (below) |
| `ConnectionsFile` | `connections.yaml` (optional; absent, empty, only comments or white space, or an explicit `null`/`~` = no connections, decision #80) | `version: 1`; `connections`, a map from a name (`^[a-z][a-z0-9_]{0,62}$`, not `lake`) to one connection |
| `ProjectError` | — | `code`, `message`, `details` (`file`, `path` as a JSON pointer into the file, `line`, `connection`, `variable`) |

Connection types (`type`):

| Type | Fields |
|---|---|
| `parquet`, `csv` | `path` (glob allowed) |
| `duckdb`, `sqlite` | `path`, `read_only` |
| `postgres` | `host`, `port`, `database`, `user`, `password`, `read_only`, optional `schema`, `ssl_mode` (`verify-full`, or `disable` on the local machine), `ssl_root_cert` (`system`, the default, or `${env:NAME}` holding a CA file's path) |
| `clickhouse` | `host`, `port`, `database`, `user`, `password`, `read_only`, optional `secure` (default `true`) |

Relative `path` values are resolved against the project folder by the loader.

## The lake's catalog (`lake.catalog`, #120)

Where the project's managed lake keeps its catalog (ADR-0027 §2). Without a `lake` section, nothing changes: a DuckDB file at `.<slug>/lake/catalog.ducklake`. Data files stay in `.<slug>/lake/data/`.

```yaml
lake:
  catalog:
    type: postgres            # duckdb (default) | sqlite | postgres
    host: db.example.com
    port: 5432                # default 5432
    database: analytics
    user: bi_lake
    password: ${env:BI_LAKE_PASSWORD}
    schema: lake_billing      # catalog tables' schema (DuckLake METADATA_SCHEMA); default public
    ssl_mode: verify-full     # required for any host other than the local machine
    ssl_root_cert: system     # default; or ${env:NAME}, a variable holding a CA file's path
```

| `type` | Fields | Serves |
|---|---|---|
| `duckdb` | optional `path` (default `.<slug>/lake/catalog.ducklake`) | one process |
| `sqlite` | optional `path` (default `.<slug>/lake/catalog.sqlite`) | several processes on one machine |
| `postgres` | `host`, `port`, `database`, `user`, `password`, optional `schema`, `ssl_mode`, `ssl_root_cert` | several processes, on one or several machines |

The loader checks, before anything is opened or created:
- **A catalog file is on a local disk.** A `duckdb` or `sqlite` `path` (relative to the project folder) is refused when it is a URL (`s3://`, `gs://`, `https://`, …) or lies on a network or remote file system, read from Linux's `/proc/self/mountinfo` for the path's deepest existing folder: `nfs`, `nfs4`, `cifs`, `smb3`, `smbfs`, `9p`, `ceph`, `glusterfs`, `lustre`, `afs` and every `fuse.*`. Both databases rely on file locking, which is unreliable there. Where the file system cannot be read (other platforms, until P9-10), only the default path is accepted.
- **Verified TLS for a remote PostgreSQL.** A host other than the local machine (`localhost`, `127.0.0.1`, `::1`, or a Unix-socket directory starting with `/`) needs `ssl_mode: verify-full`: `require` and `verify-ca` do not check that the certificate is the named host's. `ssl_root_cert` is `system` (the operating system's authorities) or `${env:NAME}`, whose value is the path of a CA file.
- **A lake is never moved silently.** Every lake, the default one included (#204), has its identity recorded in `.<slug>/lake.location`: the type, then the resolved path, or host, port, database and schema. A default lake made before #204 (its catalog file, no record) gets its record the next time a command opens it; one whose catalog file is gone while its data folder (`.<slug>/lake/data`) still holds data files is refused with `STORE_CATALOG_UNAVAILABLE`, as a recorded lake is (move `.<slug>/lake` away to start a new lake). A configuration that names another catalog for an existing lake is refused; the previous `lake.catalog` opens the same lake again. A lake made without this section is identified by its default catalog file. The record is written before the lake's first open, marked `creating`, and confirmed once the open succeeds (#146), so a stop in between never leaves a lake without a record. While a record is `creating`: the same settings open the lake and confirm it; other settings are accepted only when nothing was created there (a DuckDB or SQLite catalog file that does not exist), and are refused otherwise (`LAKE_LOCATION_CHANGED`, saying the first open did not finish; delete `.<slug>/lake.location` if no lake was created there). A PostgreSQL `host` is compared without letter case. A confirmed lake whose DuckDB or SQLite catalog file is missing (deleted, or on a disk that is not mounted) is refused at every open (`run`, `runs`, `maintain`, the scheduler) with `STORE_CATALOG_UNAVAILABLE`, naming the file: the catalog holds the lake's history, so a new, empty one is never created in its place (#192). Restore the file, or delete `.<slug>/lake.location` to start a new lake. A record that cannot be written or removed (a full disk, a read-only folder) is `LAKE_LOCATION_CHANGED`, and the lake is not opened.
- `type: mysql`, `type: quack` and a literal `password` are schema errors: those catalogs are not offered (ADR-0027 §2).

The connection's values are always quoted for libpq, so no value can add a connection keyword.

## The lake's data files (`lake.data`, `lake.encrypted`, #126, #127)

```yaml
lake:
  data:
    path: s3://bi-lake/billing/      # or a local folder (relative to the project folder)
    s3:                              # required for s3://, refused otherwise
      region: us-east-1
      endpoint: storage.example.com  # optional, an S3-compatible service (host[:port])
      url_style: path                # optional: path | vhost
      use_ssl: true                  # false only for an endpoint on the local machine
      key_id: ${env:LAKE_S3_KEY_ID}
      secret: ${env:LAKE_S3_SECRET}
  encrypted: false                   # data-file encryption; fixed when the lake is created
```

Google Cloud Storage and Azure Blob Storage (#127):

```yaml
lake:
  data:
    path: gs://bi-lake/billing/
    gcs:                             # required for gs://, refused otherwise
      key_id: ${env:LAKE_GCS_KEY_ID} # an HMAC key (Cloud Storage interoperability)
      secret: ${env:LAKE_GCS_SECRET}
```

```yaml
lake:
  data:
    path: az://bi-lake/billing/      # container, then prefix
    azure:                           # required for az://, refused otherwise
      connection_string: ${env:LAKE_AZURE_CONNECTION_STRING}
```

- **Where.** A local folder (a network file system mounted at the same path on every machine counts), S3-compatible object storage (`s3://`), Google Cloud Storage (`gs://`) or Azure Blob Storage (`az://container/prefix/`). Default: `.<slug>/lake/data/`. Each kind has one spelling: `gcs://`, `azure://`, `abfss://`, `r2://` and `http(s)://` paths are `LAKE_LOCATION_INVALID`. So are an object-storage path without its own settings block (`s3`, `gcs`, `azure`) or with another one, a path with `.` or `..` segments, and a local path with any of them. Azure Data Lake Storage Gen2 (`abfss://`) and sign-in by managed identity or service principal are not offered: they cannot be tested end to end here.
- **A folder of its own.** Maintenance deletes every `.parquet` file under the data folder that the lake does not list (DuckLake's orphan cleanup). A local data folder is therefore `LAKE_LOCATION_INVALID` when it is or contains the project folder, lies inside `.<slug>/` (other than the default), contains a file connection's file, or overlaps the folder a wildcard connection reads. An object-storage prefix cannot be checked that way: give the lake a prefix that nothing else uses.
- **Credentials** are `${env:NAME}` references only. They become a temporary DuckDB secret, scoped to the lake's prefix and never persisted. An unset variable is `PROJECT_SECRET_MISSING`.
- **HTTPS.** `use_ssl: false` is refused (`LAKE_TLS_REQUIRED`) unless the endpoint is `localhost`, `127.0.0.1` or `::1`. Google Cloud Storage is always reached at `storage.googleapis.com` over HTTPS; its block has no endpoint or TLS setting. An Azure connection string must use HTTPS (`DefaultEndpointsProtocol=https`, and an `https://` `BlobEndpoint` if it has one) unless its `BlobEndpoint` is the local machine (an emulator): otherwise `LAKE_TLS_REQUIRED`, without showing the string.
- **Encryption** (`encrypted: true`) uses DuckLake's data-file encryption. Its keys are stored in the catalog, so it protects the files only from someone without access to the catalog. Deletion-vector files would be written unencrypted, so they stay off.
- **Fixed with the lake.** A non-default data location, and encryption, are part of the recorded identity (`.<slug>/lake.location`). Changing either for an existing lake is `LAKE_LOCATION_CHANGED`; moving data is not offered.

## Secrets

`password` (and `lineage.api_key`) accepts **only** a reference `${env:NAME}` (`NAME` = `^[A-Z_][A-Z0-9_]*$`); a literal value is a schema error. The loader keeps the reference; the value is read from the environment only when a connection is used, and never appears in loaded objects, logs or errors. An unset variable is `PROJECT_SECRET_MISSING`, naming the variable.

## Verified TLS for PostgreSQL sources (#122)

A `postgres` connection on a host other than the local machine (`localhost`, `127.0.0.1`, `::1`, or a Unix-socket directory starting with `/`) must declare `ssl_mode: verify-full`: TLS that checks the server's certificate and that it belongs to the named host, so the password and the production rows never travel over a connection an impostor could answer. Without it the loader refuses the connection with `SOURCE_TLS_REQUIRED`, naming it, before anything connects. Only `verify-full` and `disable` (local machine only) are accepted; `require`, `verify-ca`, `prefer` and `allow` are schema errors. The store enforces the same rule again (`contracts/store/v1`).

## Read-only sources

`read_only` is required for database connections. `false` validates against the schema but the loader refuses it with `SOURCE_NOT_READ_ONLY`: every database source is read-only in v1.

## Checks beyond the schema (loader)

| Code | When |
|---|---|
| `PROJECT_NOT_FOUND` | the folder or its `project.yaml` does not exist, or a file is not a regular file or cannot be read |
| `PROJECT_YAML_INVALID` | not UTF-8 text, not valid YAML, more than one document, a duplicate key, an anchor, alias or merge key (`&`, `*`, `<<`), a file over 1 MiB, or values nested more than 32 levels, with its line; or a value Python cannot build (an integer over 4300 digits, a date that does not exist), with its line and path, described in words and never quoted |
| `PROJECT_VERSION_UNSUPPORTED` | `version` is an integer other than 1 |
| `PROJECT_SCHEMA_INVALID` | the file does not match its message, `time_zone` is not a zone of the bundled IANA database (system-only names such as `localtime` and the placeholder `Factory` are refused), or a connection name appears twice; a key YAML does not read as text (`null`, `1`, `true`) is named as written, with its line. An unknown top-level key is found before the document is built, so an oversized file of unknown keys is refused at once |
| `SOURCE_NOT_READ_ONLY` | a database connection declares `read_only: false` |
| `SOURCE_TLS_REQUIRED` | a `postgres` connection on another machine without `ssl_mode: verify-full` |
| `PROJECT_SECRET_MISSING` | a referenced environment variable is not set when the secret is resolved (for `lake.catalog`, when the project is validated) |
| `LAKE_LOCATION_INVALID` | a `duckdb` or `sqlite` catalog `path` that is a URL, on a network or remote file system, or not the default where the file system cannot be read; an `ssl_root_cert` variable that does not name a file; a `lake.data.path` that is neither a local folder nor `s3://`, `gs://` or `az://` with its own settings block, or a local data folder shared with the project, `.<slug>/` or a file connection |
| `LAKE_TLS_REQUIRED` | a `postgres` catalog on a host other than the local machine without `ssl_mode: verify-full`; object storage with `use_ssl: false` on an endpoint other than the local machine; an Azure connection string over plain HTTP to another machine |
| `LAKE_LOCATION_CHANGED` | the project's lake exists and `lake.catalog`, `lake.data` or `lake.encrypted` names another place than the one it was created with, or `.<slug>/lake.location` cannot be read |

## Compatibility

New optional fields, new connection types and new error codes are compatible changes. Renaming or removing a field, making an optional field required, or changing a default is breaking (new `v2`).
