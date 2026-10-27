# Module pack — `access`

**Responsibility:** authenticate callers and produce their security context; manage users, sessions, project memberships, groups and the audit log.
**Distribution / import:** `sextant-access` / `sextant.access`.

## Contracts
- **Owns:** `contracts/security-context/v1`, `contracts/audit-event/v1` (the audit log with its hash chain), instance administrator flag, personal access tokens (stored hashed; P9-02).
- **Consumes:** none.

## Required reading
| Document | Sections |
|---|---|
| `architecture/security.md` | all |
| `architecture/overview.md` | §2, §4 |
| `process/engineering.md` | §6 (security), all others apply |
| `product/laws.md` | §1–§2 |

## ADRs
0004, 0005, 0011, 0021, 0022.

## Laws that are acceptance criteria
3 (security by design), 9 (audit), anti-patterns A3, A9.

## Conformance
Security suite; OIDC integration tests against a real identity provider in a container; password hashing with `hashlib.scrypt`; threat model in every pull request.
