# Module pack — `repo` (repository tooling, CI, packaging)

**Responsibility:** everything that is not product code but keeps the product correct and shippable: repository governance, workspaces, CI checks, the identity loader and rename script, the boundary and materialization checker, version manifests, release workflows, the synthetic data generator, the Docker image and user documentation.
**Location:** `tools/`, `.github/`, root files (`identity.toml`, `versions.toml`, `pyproject.toml` files of the workspace), `docs/user/` (P10; the only part of `docs/` outside governed paths).

## Contracts
- **Owns:** none. It enforces the contracts of others (`contracts/` validation runs here) and the version manifest `versions.toml`.
- **Consumes:** `identity.toml`, every module's declared contract versions.

## Required reading
| Issue area | Document | Sections |
|---|---|---|
| All | `process/git-workflow.md` | all |
| All | `process/engineering.md` | §1, §5, §8, §9 |
| Governance, labels, milestones (P0-01) | `process/issues.md` | §7; `.github/labels.yml` |
| Versions and releases (P0-08) | `process/versioning.md` | all |
| Identity and checks (P0-04) | `architecture/overview.md` | §2, §7; ADR-0020, ADR-0025 |
| Synthetic data (P0-09, P0-10) | `product/vision.md` | §6; ADR-0020 |
| Docker and OS matrix (P9-08, P9-10) | `architecture/overview.md` | §4; ADR-0005 |
| Hardening and docs (P10) | `product/vision.md` | §5, §6; `architecture/security.md` |

## ADRs
0002, 0004, 0005, 0017, 0018, 0019, 0020, 0022, 0025.

## Laws that are acceptance criteria
4 (reproducible builds: exact pins, deterministic data generator), 8 (everything as code), anti-pattern A6 (no silent failures: every check reports what failed and where).

## Key rules
- CI scripts and checkers use the Python standard library unless a dependency is justified (ADR-0002).
- Every check created here becomes a required check from its issue onward (`engineering.md` §8) and has tests that provoke each failure it detects.
- The synthetic data generator is deterministic (fixed seed ⇒ identical files) and contains no real names, ids or values.
- Changes under `.github/workflows/` touch governed paths: `owner-merge`.
