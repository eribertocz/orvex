# 0011 — Security model: roles, groups, row and column policies, own evaluator

**Status:** Accepted (2026-10-26)

## Context
Studied: Power BI row-level security (roles with DAX filters, dynamic security with `USERPRINCIPALNAME()` and mapping tables), Tableau data policies (`USERNAME()`, `ISMEMBEROF()`), Rill (`row_filter` templated with user attributes, `access`, `include`/`exclude` columns). bi-platform chose Cedar inside the engine. Cedar's Python bindings depend on third parties.

## Decision
- An instance administrator (manages users and the instance, grants no data access) and four project layers: roles (what you can do), groups (which pages), row policies and column policies (which data) — full model in `architecture/security.md`.
- Single enforcement point: the semantic engine, for every consumer including cache, export and MCP.
- Policies are declarative YAML (`security.yaml`) evaluated by the engine's own evaluator, with attributes bound as parameters. The policy contract is designed so a Cedar-based evaluator can implement it later.
- Column security levels: visible, masked, hidden.
- Authentication: local users (`hashlib.scrypt`) and OIDC (Authorization Code + PKCE) in server mode.
- Fail closed when attributes are missing; append-only audit log with hash chain.

## Alternatives considered
- Cedar now: strong model, but adds a non-standard runtime dependency in Python.
- Security in dashboards only: violates law 3.

## Consequences
A security conformance suite with negative tests runs on every relevant pull request.
