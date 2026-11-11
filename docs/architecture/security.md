# Security model

Rank 3 in the precedence order (`docs/README.md`). Changes require Owner approval.

## 1. Principles

1. **Single enforcement point.** Every data path — dashboards, matrix, explorer, exports, cache, API, MCP — goes through the semantic engine with the caller's security context.
2. **Deny by default; fail closed.** Missing configuration or attributes result in no access, never full access.
3. **Server-side only.** The client is never trusted; hiding something in the UI is never the protection.
4. **Least privilege** for users, modules and database roles.

## 2. Identity (`access` module)

| Mode | Authentication |
|---|---|
| Local | No login. A single implicit Builder bound to the local machine (loopback only) |
| Server | Local users (password hashed with Argon2id, #438) and/or OIDC single sign-on (Authorization Code flow with PKCE and `response_mode=query`) |

- **Local mode's serve session (P2-27a, #363).** Loopback alone is not enough. Each serve session has three separate credentials, each random (256 bits), compared in constant time, kept only in the server's memory, never logged, and revoked when the server stops:
  - a **bootstrap code** for the browser, single-use and valid for a few minutes. It is written only into an owner-only launcher page, which the server opens by its path, so the code never appears on a command line. It is exchanged at `/?code=…` for a session cookie and a redirect without the code, under `Referrer-Policy: no-referrer`;
  - a **session cookie** with its own value: `HttpOnly`, `SameSite=Strict`, and a name that carries the port;
  - a **bearer token** for scripts, in a file only its owner can read, removed at shutdown.

  The `Host`, `Origin` and `Sec-Fetch-Site` checks and the `application/json` body requirement refuse DNS rebinding and cross-site requests before any project work. The local Builder's context comes from `access` (`LocalContextProvider`), and only for an authenticated request; the CLI's local builder grant never reaches HTTP.

  **Limits:**
  - The files' permissions protect against other users of the machine. They do not isolate the server from a malicious process of the same user, which can read that user's files and memory; only isolation by the operating system (another user, a container, a sandbox) does.
  - Browsers do not keep cookies apart by port. The session cookie is therefore also sent to any other server on 127.0.0.1 the user opens, including another user's; over plain-HTTP loopback nothing prevents it.
- Users are global to the instance. Users are identified by an internal id; with OIDC, by issuer + subject, never by email.
- **Server mode's local users (P3-04, #438).**
  - **Passwords.** Argon2id (RFC 9106) with at least OWASP's parameters (m = 19 MiB, t = 2, p = 1), stored in each hash; a weaker hash is replaced at the next sign-in. At least 12 characters, at most 1,024, no composition rules, never the user name; normalized to NFKC.
  - **Temporary passwords.** A user created or reset by an administrator, and the first administrator (`admin create`), gets a random one, shown once. It expires after 24 hours, and its session may only change it (`PASSWORD_CHANGE_REQUIRED`). There is no self-service recovery by e-mail in v1.
  - **Failed sign-ins.** A delay per account and per address (IPv4, or IPv6 /64), doubling from 1 s up to 15 minutes, never a permanent lock-out. An unknown user gets the same answer as a wrong password, after the same Argon2id work. The counters are bounded and stored as hashes. `X-Forwarded-For` counts only from the administrator's `trusted_proxies`. Every failure is audited.
- Sessions are server-side: a random 256-bit id in the `__Host-<slug>_session` cookie (`HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`), only its hash in the store. A session ends after its absolute lifetime (12 h) or idle lifetime (1 h), both the administrator's, and at once at sign-out, deactivation, a change to the user's permissions (`authz_version`) or a password change or reset. Every request re-reads the user.
- A state-changing request (any method but `GET`, `HEAD` and `OPTIONS`) needs `Origin` equal to the instance's public origin, `Sec-Fetch-Site: same-origin` when sent, and an `application/json` body: `ORIGIN_REFUSED` otherwise. No route takes both a safe and a state-changing method.
- The OIDC flow uses a separate short-lived binding cookie (`state`, `nonce`, PKCE verifier; at most 10 minutes) with `SameSite=Lax`, because the identity provider's redirect back is a cross-site navigation. It is deleted when the session cookie is issued and is never accepted as a session.
- **OIDC sign-in (P3-05, #439).**
  - **Linking.** An identity signs in only once an administrator has linked it to an existing user (Q1 A); nothing links itself (Q5 A).
  - **ID tokens.** The ID token is validated with an algorithm allow-list fixed by configuration (`RS256`, `PS256`, `ES256`, `EdDSA`; never `none` or `HS*`), and checked for `iss`, `aud`, `azp`, `exp`, `iat`, `nbf` (60 s of skew) and `nonce`. The `iss` response parameter is checked against mix-up. Every failure is one generic `OIDC_FAILED`; the audit log names the step.
  - **Keys.** Keys are read again for an unknown `kid` at most once a minute per provider.
  - **Provider groups.** They are trusted for at most `group_refresh` (1 hour by default, at most 24). Then a silent re-authentication (`prompt=none`) sets them again, and a failure ends the session. A mapping change, an unlink or a deactivation ends the user's sessions at their next request.
  - **Requests to a provider (SSRF).** HTTPS only, the certificate and host name verified, no redirects, no environment proxy, 5 s for the whole exchange and at most 1 MiB. The token and key endpoints must be on the issuer's host unless listed. The host is resolved once, every address is checked, and the connection goes to the checked address. Addresses that are not globally routable are refused (loopback, link-local, private, carrier-grade NAT, multicast, IPv4-mapped forms included); an `internal` provider may reach only the private networks listed for it.
- OIDC group claims can map to Orvex groups only through an explicit mapping in instance configuration; a claim alone grants nothing.

## 3. Authorization layers

| Layer | Question it answers | Defined in | Enforced by |
|---|---|---|---|
| **Instance administrator** | Who manages the instance itself? | Instance state (`access`) | `server` |
| **Role** (per project) | What can the user **do**? | Instance state (`access`) | `server`, `dashboards`, `semantic` |
| **Group** (per project) | Which **pages** can the user see? | Membership in `access`; page lists in page YAML | `dashboards` (single page enforcer) |
| **Row policy** | Which **rows** can the user see? | `security.yaml` | `semantic` |
| **Column policy** | Which **fields and metrics** can the user see? | `security.yaml` | `semantic` |

### 3.1 Roles

The **instance administrator** is an instance-level flag, not a project role: it creates and deactivates users, registers projects with the instance, configures OIDC and the claim-to-group mapping, and assigns each project's first Builder. It grants **no data access**: an administrator sees a project's data only through a project role. The first administrator is created by `orvex admin create` on the server machine; local mode has a single implicit user who is administrator and Builder.


| Permission | Builder | Author | Viewer |
|---|---|---|---|
| View published dashboards and pages allowed by groups | ✅ | ✅ | ✅ |
| Use the explorer on certified metrics | ✅ | ✅ | ❌ |
| Create and edit dashboards (drafts, publish) | ✅ | ✅ | ❌ |
| Propose metrics | ✅ | ✅ | ❌ |
| Certify metrics; edit the semantic model | ✅ | ❌ | ❌ |
| Edit models, connections, runs | ✅ | ❌ | ❌ |
| Manage project memberships, groups and policies (users themselves are managed by the instance administrator) | ✅ | ❌ | ❌ |
| Export data | ✅ | ✅ | Configurable per dashboard (`export.viewers: true`, default `false`, in `dashboard.yaml`) |
| Use MCP | ✅ | ✅ | Configurable per project (`mcp.viewers: true`, default `false`, in `project.yaml`) |

### 3.2 Page access

```yaml
# dashboards/consumption/pages/arrears.yaml
version: 1
title: Arrears
access:
  groups: [management, collections]     # omitted ⇒ every project member with view permission
```

- **`dashboards` is the single enforcer of page access.** `server` asks `dashboards` before returning a page definition **and** before passing any query that carries a page reference to `semantic`. Denied ⇒ `403` with code `PAGE_FORBIDDEN`, audited. `semantic` knows nothing about pages; it enforces rows and columns.
- **Page-bound queries are derived, not trusted.** The browser sends `{page, visual_id, interaction_state}` (filter values, drill level, cross-filter selections, sort, viewport). `server` asks `dashboards` to build the semantic query from the **published** visual specification plus the interaction state; members, metrics and filter fields not present in that visual or page are impossible to request. A Viewer therefore cannot use an allowed page to run arbitrary queries. Invalid interaction state ⇒ `PAGE_FORBIDDEN`.
- Free-form semantic queries (explorer, metric preview) require the role permission for that use (§3.1); MCP follows §5. Row and column policies always apply.
- Navigation (sidebar) shows only allowed pages.

### 3.3 Row and column policies

```yaml
# security.yaml
version: 1
row_policies:
  - name: own_zone_only
    dataset: readings
    predicate: "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"
    exempt_groups: [management]          # these groups see all rows
column_policies:
  - member: consumers.national_id
    default: hidden                      # visible | masked | hidden
    visible_to_groups: [collections]
  - member: consumers.phone
    default: masked
    mask: "***"
```

- `$user_id` and other attributes are bound parameters from the security context, never string-concatenated. `$user_id` is always the context's own user: no attribute may set it.
- **Where attributes come from (P3-06, #440, option A).** They are per project and typed: `text`, `integer`, `boolean` or `list`, with no coercion. A project's Builder sets them for another active member, never for themselves; the administrator flag grants no attribute rights. Every change is audited, and they end with the membership. A stored value that fails its checks builds no context (`ACCESS_FORBIDDEN`). Resolution goes through an `AttributeSource` interface, so a later source (an identity provider's claims) can be added behind a fixed precedence: a value set by hand wins.
- **The server-mode context** is built at each request from one read-only transaction of the directory. Its fingerprint covers the user, project, role, groups, attributes, the policy model and the snapshot of the managed-lake tables the policies read; the query runs at that same snapshot. A next page reads its data at its cursor's snapshot but is checked against the permissions in force now: the current policies, and the cursor's snapshot only while no table the policies read has changed since. A next page whose fingerprint differs, or whose current permissions cannot be read, is `PAGE_CURSOR_INVALID`. Errors and explanations are written for the caller's audience, `builder` only for a Builder.
- Mapping tables (for example `security.user_zone`) live in the managed DuckLake and are maintained by the Builder or by a transformation model; a policy reading a table elsewhere is rejected (ADR-0027 §6).
- A dataset with a row policy and a caller with no mapping ⇒ zero rows, with the generic notice `POLICY_MAPPING_MISSING`. As for a missing attribute, it never names the policy, table or values to an ordinary user (Owner decision of 2026-11-02, #123).
- **Missing attribute.** If a predicate references an attribute the security context lacks (for example `$zone_id` for a user without it), the policy evaluates to *no rows* (fail closed, never an error that reveals the predicate and never all rows). The result carries a visible notice with code `POLICY_ATTRIBUTE_MISSING`. Ordinary users get a generic sentence ("You have no access assigned for this query. Request authorization.") that names no policy, table or value. Only authorized staff see the policy in the detailed diagnosis. An audit event is recorded (Owner decision of 2026-11-02, #304).
- Only the policies in `security.yaml` exist; groups referenced there are the same groups used for pages.
- **Masking rules** (`masked` applies to dimensions and fields only; metrics are `visible` or `hidden`):
  - a masked member is returned as text (`VARCHAR`) containing the mask, whatever its source type; the result schema says so;
  - grouping, filtering, sorting, distinct counting and possible values on a masked member are rejected with `MEMBER_MASKED`, because they would reveal cardinality or order of the real values;
  - a masked member may appear only in detail-row queries and exports, always masked;
  - a `hidden` member does not exist for the caller: it is absent from the catalog, and referencing it fails with `MEMBER_NOT_FOUND` (never a different error that confirms it exists).
- Policies apply before aggregation on every level: details, subtotals, totals, distinct counts, possible values, pre-aggregations, exports and MCP.

## 4. Audit log

- Records authentication events, page views, every semantic query (with its receipt, not raw values), exports, MCP calls, permission changes and model certifications.
- Append-only, with a hash chain so tampering is detectable.
- Retention configured by the user (default 2 years).

## 5. AI access (MCP)

- MCP tools act **as the authenticated user**: same roles, groups, row and column policies.
- **Transports:** in local mode, **stdio** (the AI client starts `orvex mcp <project>` and acts as the local user); in server mode, **Streamable HTTP** at `/mcp`, authenticated with a **personal access token** the user creates in Orvex (Bearer token, stored hashed, scoped to one project, expiring, revocable, audited). OAuth 2.1 authorization for MCP is a later improvement and does not change the tools.
- `list_dashboards` returns only pages the user may see; the decision comes from `dashboards` through `server`.
- AI never sends SQL; only semantic queries.
- Read-only by default; every call is audited.

## 6. Security conformance suite

`conformance/security/` contains negative tests that must always fail to access data: page access bypass, row-policy removal through filters, LOD and context modifiers, cache reuse across users with different permissions, export of hidden columns, possible-values leakage, MCP queries outside permissions, missing-attribute fail-closed, and the Direct Query cases of ADR-0027 §6 on every enabled engine. It runs on every pull request that touches `semantic`, `access`, `dashboards`, `mcp` or `server`.
