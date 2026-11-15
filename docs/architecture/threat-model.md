# Threat model

Rank 3 in the precedence order (`docs/README.md`), with `security.md`. Changes require Owner approval.

One STRIDE model for every surface reachable today. Each threat is tied to the rule that answers it and to the test that proves it, or it is declared as a gap. The rule that keeps the model current is in `security.md` §7.

**How to read a row.**

| Column | Meaning |
|---|---|
| `ID` | `TM-<SURFACE>-<n>`. Unique, never reused. A removed row leaves its number unused. |
| `Category` | One of the six STRIDE categories: Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege. A category that does not apply to a surface still has one row, whose `Verification` is `n/a: <reason>`. |
| `Rule` | A section of `security.md` (`security.md §2`), an ADR (`ADR-0027`), or a law (`laws.md Law 3`). |
| `Verification` | An existing test or `conformance/security/scenarios/` path, the roadmap ID that will add it (`roadmap:P3-01`), `gap: <reason>`, or `n/a: <reason>` when the threat does not apply to the surface. |

## Inventory

The check reads this block. It holds one line per item: `<kind> <name> <surface | internal: reason>`.
- `http` is each `paths` key of `contracts/http-api/v1/openapi.yaml`.
- `package` is each directory under `packages/`.
- `contract` is each directory under `contracts/` except `_example`.

```inventory
http /api/v1/health http-api
http /api/v1/version http-api
http /api/v1/semantic/query http-api
http /api/v1/semantic/jobs/{job} http-api
http /api/v1/semantic/explain http-api
http /api/v1/semantic/catalog http-api
http /api/v1/auth/login http-api
http /api/v1/auth/session http-api
http /api/v1/auth/logout http-api
http /api/v1/auth/password http-api
http /api/v1/auth/oidc/{provider}/start http-api
http /api/v1/auth/oidc/{provider}/callback http-api
http /api/v1/admin/users http-api
http /api/v1/admin/users/{user} http-api
http /api/v1/admin/users/{user}/deactivate http-api
http /api/v1/admin/users/{user}/reactivate http-api
http /api/v1/admin/users/{user}/administrator http-api
http /api/v1/admin/users/{user}/password-reset http-api
http /api/v1/admin/users/{user}/identities http-api
http /api/v1/admin/users/{user}/identities/{link} http-api
http /api/v1/admin/user-lookup http-api
http /api/v1/admin/projects/{project}/members http-api
http /api/v1/admin/projects/{project}/members/{user} http-api
http /api/v1/admin/projects/{project}/groups http-api
http /api/v1/admin/projects/{project}/groups/{group} http-api
http /api/v1/admin/projects/{project}/groups/{group}/members/{user} http-api
http /api/v1/admin/projects/{project}/members/{user}/attributes http-api
http /api/v1/admin/projects/{project}/members/{user}/attributes/{name} http-api
package access access
package contracts internal: schemas and types only, reached through the surfaces that load them
package dashboards internal: not reachable until P6
package mcp internal: not reachable until P9
package semantic http-api
package server http-api
package store lake
package transform project
contract audit-event access
contract catalog http-api
contract http-api http-api
contract maintenance-record lake
contract model-config project
contract ossie-extensions project
contract project project
contract receipt http-api
contract run-record project
contract security-context access
contract security-policies project
contract semantic-error http-api
contract semantic-explain http-api
contract semantic-query http-api
contract semantic-result http-api
contract store sources
```

## Surface: http-api

The HTTP API of `contracts/http-api/v1`, served by `server`, in local mode (loopback, serve session) and in server mode.

- **Assets:** project data through semantic answers; the catalog of members a caller may see; sessions and bearer tokens; the instance's users, groups, memberships and attributes; the audit log.
- **Trust boundaries:** the browser or script and the server; the reverse proxy and the server (`trusted_proxies`); the server and the identity provider (OIDC).
- **Entry points:**
  - **Web build:** the static files and client routes `server` serves outside `/api/`.
  - **Public:** `/api/v1/health` and `/api/v1/version`.
  - **Semantic:** `/api/v1/semantic/query`, `/api/v1/semantic/jobs/{job}`, `/api/v1/semantic/explain` and `/api/v1/semantic/catalog`.
  - **Authentication:** `/api/v1/auth/login`, `/api/v1/auth/session`, `/api/v1/auth/logout`, `/api/v1/auth/password`, `/api/v1/auth/oidc/{provider}/start` and `/api/v1/auth/oidc/{provider}/callback`.
  - **Administration:** `/api/v1/admin/users`, `/api/v1/admin/users/{user}`, `/api/v1/admin/users/{user}/deactivate`, `/api/v1/admin/users/{user}/reactivate`, `/api/v1/admin/users/{user}/administrator`, `/api/v1/admin/users/{user}/password-reset`, `/api/v1/admin/users/{user}/identities`, `/api/v1/admin/users/{user}/identities/{link}` and `/api/v1/admin/user-lookup`.
  - **Project administration:** `/api/v1/admin/projects/{project}/members`, `/api/v1/admin/projects/{project}/members/{user}`, `/api/v1/admin/projects/{project}/groups`, `/api/v1/admin/projects/{project}/groups/{group}`, `/api/v1/admin/projects/{project}/groups/{group}/members/{user}`, `/api/v1/admin/projects/{project}/members/{user}/attributes` and `/api/v1/admin/projects/{project}/members/{user}/attributes/{name}`.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-HTTP-1 | Spoofing | In local mode, a request without the serve session's cookie or bearer token acts as the local Builder, or one credential is accepted for another. | security.md §2 | packages/server/tests/test_semantic_http.py |
| TM-HTTP-2 | Spoofing | In server mode, a request without a valid session reaches a route. | security.md §2 | packages/server/tests/test_server_roles_limits.py |
| TM-HTTP-3 | Spoofing | DNS rebinding or a cross-site page reaches the local-mode server on loopback. | security.md §2 | packages/server/tests/test_app.py |
| TM-HTTP-4 | Tampering | A cross-site request changes state (CSRF). | security.md §2 | packages/server/tests/test_sign_in_http.py |
| TM-HTTP-5 | Tampering | A semantic query reaches an undeclared member, or a parameter or value of the wrong type, through a member name, a parameter or a filter. | security.md §3.3 | packages/semantic/tests/test_query_binding.py |
| TM-HTTP-18 | Tampering | A text filter or parameter value is inlined into the compiled SQL and runs as SQL (injection). | security.md §3.3 | gap: values are bound by design, but no test asserts that a sentinel value is absent from the compiled SQL text and present in its parameters; the option is that test on every engine adapter |
| TM-HTTP-6 | Tampering | A request for a static file or a client route escapes the web build's folder (path traversal). | security.md §9 | packages/server/tests/test_app.py |
| TM-HTTP-7 | Repudiation | A caller denies having asked a question, or an answer is sent without a record. | security.md §4 | packages/server/tests/test_server_delivery.py |
| TM-HTTP-8 | Repudiation | A value written to the server's log forges a second log line. | security.md §9 | gap: the log is JSON lines, but no test asserts that a newline or control character in a logged value stays inside its line; the option is a test on JsonFormatter with such values |
| TM-HTTP-9 | Information disclosure | A query, filter, possible-values request or cursor reveals rows outside the caller's row policies. | security.md §3.3 | conformance/security/scenarios/rows |
| TM-HTTP-10 | Information disclosure | A hidden or masked member is revealed through the catalog, an error, grouping or sorting. | security.md §3.3 | conformance/security/scenarios/columns |
| TM-HTTP-11 | Information disclosure | A cached answer is served to a caller with different permissions. | ADR-0012 | conformance/security/scenarios/cache |
| TM-HTTP-12 | Information disclosure | An error message names a policy, a table or a value to an ordinary user. | security.md §3.3 | conformance/security/scenarios/missing-attributes |
| TM-HTTP-13 | Information disclosure | A page-bound query carries members or filters that are not in the published visual. | security.md §3.2 | roadmap:P6-08 |
| TM-HTTP-14 | Denial of service | A caller exhausts the engine with long or concurrent questions. | security.md §3.1 | packages/server/tests/test_server_roles_limits.py |
| TM-HTTP-15 | Denial of service | A large request body exhausts the server's memory before any check. | laws.md Law 7 | gap: no request body size limit is applied before parsing; the options are a limit at the server, or a documented limit at the reverse proxy, which leaves direct exposure unprotected |
| TM-HTTP-16 | Elevation of privilege | A route answers without declaring and checking its permission. | security.md §3.1 | packages/server/tests/test_server_roles_limits.py |
| TM-HTTP-17 | Elevation of privilege | A lowered role or a removed permission does not reach a session that is already open. | security.md §2 | packages/server/tests/test_server_roles_limits.py |

## Surface: access

Identity and authorization state in `access`: local users, OIDC links, sessions, the directory (users, groups, memberships, attributes) and the audit log. Its HTTP entry points are the `auth` and `admin` paths of the `http-api` surface. Its other entry points are the `orvex admin` commands and the directory's database (SQLite or PostgreSQL).

- **Assets:** password hashes; session ids; OIDC links and provider groups; memberships and attributes that build each security context; the audit log and its signing key.
- **Trust boundaries:** the user and the server; the instance administrator and the projects' data; the server and the identity provider; the server and the directory's database.
- **Entry points:** the `auth` and `admin` paths listed under `http-api`; `orvex admin create` and the other `orvex admin` commands; the directory database. The OIDC routes allow no open redirect: the start route takes only the provider, and the callback always opens `/`.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-ACCESS-1 | Spoofing | An attacker guesses a password, or learns whether a user name exists. | security.md §2 | packages/access/tests/authentication/test_sign_in_delays.py |
| TM-ACCESS-2 | Spoofing | A forged or replayed ID token, or a provider mix-up, signs in as another user. | security.md §2 | packages/access/tests/oidc/test_oidc_sign_in.py |
| TM-ACCESS-3 | Spoofing | A request to the identity provider is redirected to an internal address (SSRF). | security.md §2 | packages/server/tests/test_provider_fetch.py |
| TM-ACCESS-4 | Spoofing | A session id planted before sign-in stays valid after it (session fixation). | security.md §2 | packages/access/tests/authentication/test_sign_in_sessions.py |
| TM-ACCESS-5 | Tampering | The audit log is edited or truncated without detection. | security.md §4 | packages/access/tests/audit/test_audit_tampering.py |
| TM-ACCESS-6 | Tampering | A stored attribute value that fails its checks builds a security context. | security.md §3.3 | packages/access/tests/directory/test_directory_attributes.py |
| TM-ACCESS-7 | Repudiation | The administrator denies resetting a user's password (`user.password_reset`). | security.md §8 | packages/server/tests/test_admin_http.py |
| TM-ACCESS-8 | Repudiation | The administrator denies linking an OIDC identity to a user (`user.oidc_linked`). | security.md §8 | packages/access/tests/oidc/test_oidc_sign_in.py |
| TM-ACCESS-9 | Repudiation | The administrator denies creating a user and making it a project's first Builder (`user.created`, `membership.added`). | security.md §8 | packages/access/tests/audit/test_audit_log.py |
| TM-ACCESS-10 | Repudiation | A failed password sign-in leaves no record (`auth.login_failed`). | security.md §4 | packages/access/tests/authentication/test_sign_in_passwords.py |
| TM-ACCESS-11 | Repudiation | A failed OIDC sign-in leaves no record (`auth.login_failed`). | security.md §4 | packages/access/tests/oidc/test_oidc_sign_in.py |
| TM-ACCESS-12 | Information disclosure | A session id or password reaches the store or a log in clear. | security.md §2 | packages/access/tests/authentication/test_sign_in_sessions.py |
| TM-ACCESS-13 | Denial of service | Failed sign-ins lock a legitimate user out permanently. | security.md §2 | packages/access/tests/authentication/test_sign_in_delays.py |
| TM-ACCESS-14 | Elevation of privilege | The administrator resets a Builder's, Author's or Viewer's password and signs in with the temporary password, gaining that user's data access. | security.md §8 | gap: allowed and audited, not refused. Options for the Owner: (a) the target confirms the reset or is notified, which needs a contact channel v1 lacks; (b) the reset cannot sign in until the user sets a password through a separate channel, which needs that channel; (c) a two-person rule or break-glass flag for a user with a project role, which needs a second administrator; (d) an alert on `user.password_reset`, which detects but does not prevent; (e) accept the documented risk, which leaves the path open and audited |
| TM-ACCESS-15 | Elevation of privilege | The administrator links an OIDC identity they control to an existing user and signs in through the provider. | security.md §8 | gap: allowed and audited, not refused. Options for the Owner: (a) the target confirms the link or is notified, which needs a contact channel v1 lacks; (b) a two-person rule or break-glass flag for linking a user with a project role, which needs a second administrator; (c) an alert on `user.oidc_linked`, which detects but does not prevent; (d) accept the documented risk, which leaves the path open and audited |
| TM-ACCESS-16 | Elevation of privilege | The administrator creates a user they control and assigns it as a project's first Builder. | security.md §8 | gap: allowed and audited, not refused. Options for the Owner: (a) a two-person rule for the first Builder of a project that already holds data, which needs a second administrator; (b) an alert on `user.created` followed by `membership.added` with the Builder role, which detects but does not prevent; (c) accept the documented risk, which leaves the path open and audited |
| TM-ACCESS-17 | Elevation of privilege | A Builder sets attributes for themselves to widen their own rows. | security.md §3.3 | packages/access/tests/directory/test_directory_attributes.py |
| TM-ACCESS-18 | Elevation of privilege | A provider group claim grants an Orvex group without an explicit mapping. | security.md §2 | packages/access/tests/oidc/test_oidc_sign_in.py |

## Surface: project

The project folder and the CLI: model SQL and its sidecars, `project.yaml`, `connections.yaml`, `security.yaml`, and secrets read from the environment.

- **Assets:** the model definitions and policies (what is computed and who sees it); connection secrets; the run records.
- **Trust boundaries:** the project's author and the server that reads the folder; the folder and the environment that holds secrets.
- **Entry points:** the project files; `orvex` commands; environment variables named by `connections.yaml`.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-PROJECT-1 | Spoofing | A project file placed as a symbolic link is read as part of the project. | ADR-0007 | packages/server/tests/test_regular_file.py |
| TM-PROJECT-2 | Spoofing | A model file placed as a symbolic link is followed during discovery. | ADR-0007 | packages/transform/tests/test_models.py |
| TM-PROJECT-3 | Tampering | A model's SQL writes, attaches another catalog or reads files outside the lake. | ADR-0031 | packages/store/tests/test_lake.py |
| TM-PROJECT-4 | Tampering | A model's SQL reaches the managed lake from a source reader. | ADR-0031 | packages/store/tests/test_source_isolation.py |
| TM-PROJECT-5 | Tampering | An invalid `security.yaml` loads and leaves a dataset without its policy. | security.md §1 | packages/semantic/tests/test_policies.py |
| TM-PROJECT-6 | Repudiation | A run leaves no record of what it read and wrote. | ADR-0015 | packages/server/tests/test_run_records.py |
| TM-PROJECT-7 | Information disclosure | A connection secret appears in an error. | security.md §9 | packages/store/tests/test_redaction.py |
| TM-PROJECT-8 | Information disclosure | A connection secret reaches a run record. | security.md §9 | packages/server/tests/test_run_records.py |
| TM-PROJECT-9 | Information disclosure | A literal password in `connections.yaml` is accepted or echoed back. | security.md §9 | packages/server/tests/test_project.py |
| TM-PROJECT-10 | Denial of service | An oversized or deeply nested project file exhausts the server that loads it, which in server mode is shared by every project. | laws.md Law 7 | packages/server/tests/test_project.py |
| TM-PROJECT-11 | Denial of service | A file that never ends, such as a pipe named like a model, hangs discovery. | laws.md Law 7 | packages/transform/tests/test_models.py |
| TM-PROJECT-12 | Elevation of privilege | The CLI's local builder grant reaches an HTTP request. | security.md §2 | packages/access/tests/test_local_context.py |

## Surface: lake

The managed DuckLake catalog and data location (SQLite or PostgreSQL catalog, local or object storage), and existing DuckLakes attached read-only.

- **Assets:** the lake's tables and snapshots; the mapping tables the row policies read; the catalog's credentials.
- **Trust boundaries:** the server and the catalog database; the server and the data location; the managed lake and every existing lake.
- **Entry points:** the catalog database; the data files; the declaration of an existing lake.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-LAKE-1 | Spoofing | An existing lake is declared over a location that holds the managed lake's files. | ADR-0027 | packages/store/tests/test_existing_lake_files.py |
| TM-LAKE-2 | Tampering | A file added inside or outside an existing lake's location is read as its data. | ADR-0027 | packages/store/tests/test_existing_lake_files.py |
| TM-LAKE-3 | Repudiation | A maintenance operation on the lake leaves no record. | ADR-0015 | packages/server/tests/test_maintenance.py |
| TM-LAKE-4 | Information disclosure | An existing lake's catalog role reads schemas outside its own, directly or through an invoker view. | ADR-0027 | packages/store/tests/test_existing_lake.py |
| TM-LAKE-5 | Information disclosure | A row policy reads a mapping table outside the managed lake. | ADR-0027 | packages/semantic/tests/test_policies.py |
| TM-LAKE-6 | Denial of service | A read of the managed lake exhausts the server's memory or time. | laws.md Law 7 | packages/store/tests/test_budgets.py |
| TM-LAKE-7 | Elevation of privilege | An existing lake declared as read-only is written through. | ADR-0027 | packages/store/tests/test_existing_lake.py |

## Surface: sources

Source and Direct Query connections: PostgreSQL, ClickHouse and files.

- **Assets:** the source's rows; the connection credentials.
- **Trust boundaries:** the server and each source; a source's answer and the engine.
- **Entry points:** each connection of `connections.yaml`; each Direct Query read.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-SOURCES-1 | Spoofing | A source's server is impersonated on the network. | ADR-0027 | packages/store/tests/test_source_tls.py |
| TM-SOURCES-2 | Tampering | A ClickHouse read changes its own settings or runs a second statement. | ADR-0028 | packages/store/tests/test_clickhouse.py |
| TM-SOURCES-3 | Repudiation | A source read leaves no record. | security.md §4 | n/a: not applicable, because sources are read only and every read is recorded by the semantic answer or run that made it (TM-HTTP-7, TM-PROJECT-6) |
| TM-SOURCES-4 | Information disclosure | A Direct Query read bypasses the caller's row or column policies. | ADR-0027 | conformance/security/scenarios/direct-query |
| TM-SOURCES-5 | Denial of service | A source view that exhausts memory or runs forever stops the server. | laws.md Law 7 | packages/store/tests/test_source_isolation.py |
| TM-SOURCES-6 | Denial of service | A ClickHouse read over its time budget keeps running on the source. | laws.md Law 7 | packages/store/tests/test_clickhouse.py |
| TM-SOURCES-7 | Elevation of privilege | A source connection reaches files or networks outside its declared scope. | ADR-0031 | packages/store/tests/test_source_isolation.py |

## Surface: build

Build and release: dependencies, workflows and release artifacts (supply chain, #242).

- **Assets:** the released wheels and image; the signing identity; the repository's workflows.
- **Trust boundaries:** the repository and its dependencies; the workflows and the actions they run; the release and its users.
- **Entry points:** dependency updates; pull requests; workflow actions; the release workflow.

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
| TM-BUILD-1 | Spoofing | A released artifact is replaced by one that is not attested. | security.md §9 | tests/tools/test_release_signing.py |
| TM-BUILD-2 | Tampering | A workflow action changes under the same reference. | security.md §9 | tests/tools/test_ci_alert_workflows.py |
| TM-BUILD-3 | Repudiation | A release ships without a record of what it contains. | security.md §9 | tests/tools/test_sbom.py |
| TM-BUILD-4 | Information disclosure | A secret reaches the repository or a log. | security.md §9 | gap: no secret scanner runs on pull requests; the options are a scanner as a required check, which adds a dependency, or GitHub secret scanning with push protection, which depends on the repository's plan |
| TM-BUILD-5 | Denial of service | An outage of the build stops a release. | security.md §9 | n/a: not applicable, because the build runs on hosted runners the project does not operate, and an outage delays a release without exposing anything |
| TM-BUILD-6 | Elevation of privilege | A pull request's workflow gains write access to the repository or its secrets. | security.md §9 | tests/tools/test_ci_checks.py |
