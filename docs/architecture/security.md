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
| Server | Local users (password hashed with `hashlib.scrypt`) and/or OIDC single sign-on (Authorization Code flow with PKCE and `response_mode=query`) |

- Users are global to the instance. Users are identified by an internal id; with OIDC, by issuer + subject, never by email.
- Sessions are server-side, in `HttpOnly`, `Secure` (HTTPS), `SameSite=Strict` cookies; mutating requests validate `Origin`.
- The OIDC flow uses a separate short-lived binding cookie (`state`, `nonce`, PKCE verifier; at most 10 minutes) with `SameSite=Lax`, because the identity provider's redirect back is a cross-site navigation. It is deleted when the session cookie is issued and is never accepted as a session.
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

- `$user_id` and other attributes are bound parameters from the security context, never string-concatenated.
- Mapping tables (for example `security.user_zone`) live in the managed DuckLake and are maintained by the Builder or by a transformation model; a policy reading a table elsewhere is rejected (ADR-0027 §6).
- A dataset with a row policy and a caller with no mapping ⇒ zero rows.
- **Missing attribute.** If a predicate references an attribute the security context lacks (for example `$zone_id` for a user without it), the policy evaluates to *no rows* (fail closed, never an error that reveals the predicate and never all rows). The result carries a visible notice with code `POLICY_ATTRIBUTE_MISSING` naming the policy, not the data, so the user knows to ask an administrator, and an audit event is recorded.
- Only the policies in `security.yaml` exist; groups referenced there are the same groups used for pages.
- **Masking rules** (`masked` applies to dimensions and fields only; metrics are `visible` or `hidden`):
  - a masked member is returned as text (`VARCHAR`) containing the mask, whatever its source type; the result schema says so;
  - grouping, filtering, sorting, distinct counting and possible values on a masked member are rejected with `MEMBER_MASKED`, because they would reveal cardinality or order of the real values;
  - a masked member may appear only in detail-row queries and exports, always masked;
  - a `hidden` member does not exist for the caller: it is absent from the catalog, and referencing it fails with `MEMBER_NOT_FOUND` (never a different error that confirms it exists).
- Policies apply before aggregation on every level: details, subtotals, totals, distinct counts, possible values, pre-aggregations, exports and MCP.

### 3.4 Protection across planning, execution and errors

Policies stay effective through every transformation (ADR-0029 §7):
- A policy dataset enters a plan only through `SecureScan`, and joins that propagate a policy are immovable.
- Rewrites come from a closed catalog, each proven to keep every `SecureScan`'s visible rows unchanged.
- An independent validator checks the plan, and a second check inspects the generated SQL.
- Engine-side row policies receive the same values where the engine has them.
- Metamorphic tests on the real engines show that rows invisible to the caller change nothing the caller sees, across optimizer settings.

**No error reveals anything unauthorized:**
- not the names of models, datasets, tables, columns, members, policies or expressions;
- not SQL text or values.

Validation errors name only members the caller can see. Data-dependent execution errors are shown in full only when the caller's effective permissions cover every row and column the statement read; otherwise the caller gets a generic message with a correlation id, and the detail goes to the audit log.

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
