# Versioning and releases

## 1. Standard

Sextant follows [Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH[-PRERELEASE]`.

- **MAJOR** — incompatible change to a public contract.
- **MINOR** — backwards-compatible capability.
- **PATCH** — backwards-compatible fix.
- **PRERELEASE** — `alpha.N`, `beta.N` or `rc.N`.

While the product version is `0.y.z`, contracts may change between minor versions (with an ADR). From `1.0.0`, the public contracts are guaranteed: a breaking change requires `2.0.0`.

## 2. What each stage guarantees

"Alpha" and "beta" describe the **stability of formats and contracts**, never correctness or security. The laws apply from the first line of code: a wrong number or a data leak is unacceptable at every stage.

| Stage | Tag form | Used by | Guarantees |
|---|---|---|---|
| **Alpha** | `v0.N.0-alpha.K` | Owner only | Laws met; formats (project YAML, API, contracts) **may still change** between phases |
| **Beta** | `v0.N.0-beta.K` | Pilot users (manager and one or two department heads) | Full MVP scope implemented; formats **frozen** except for defects |
| **Release candidate** | `v1.0.0-rc.K` | Pilot users | Release criteria met; only defect fixes allowed |
| **Stable** | `v1.0.0` | All intended users | Contracts guaranteed by SemVer |

**The MVP is released as `v1.0.0` (stable).** Every alpha, beta and release candidate is a step toward it.

## 3. Module versions and product versions

Each module is versioned independently, and the product is a manifest of module versions.

| Version | Tag | Example |
|---|---|---|
| Module | `<module>-v<semver>` | `semantic-v0.3.0`, `transform-v0.2.1` |
| Product | `v<semver>` | `v0.3.0-alpha.1` |

- The file `versions.toml` at the repository root lists the module versions that compose each product version. The release PR updates it.
- Module tags never carry a prerelease suffix: while a module is `0.y.z` it is unstable by definition, and the product tag carries the stage (alpha, beta, rc).
- Every module starts at `0.1.0` in the first product release that contains it.
- A module's version changes only when that module changes.
- Each module declares the contract versions it supports (`contracts/<contract>/vN`). The server refuses to start with an incompatible combination and reports which contract and versions conflict.
- Contract versions are independent of module versions. A breaking contract change creates a new contract major version; the producing module supports the previous version during a transition period defined in the ADR.

## 4. Release criteria for v1.0.0

All must be evidenced in the release issue:

1. Semantic corpus: all 21 SEF-derived cases pass, plus every case added for capabilities S1–S6.
2. Security conformance suite (page, row, column, cache, export, AI) passes, including negative cases.
3. Every MVP capability in `product/vision.md` §5 works end to end on the synthetic reference dataset and on the Owner's real data (validated locally by the Owner; real data never enters the repository).
4. Non-functional targets in `product/vision.md` §6 are measured and met.
5. Accessibility: automated checks pass on every screen; manual keyboard and screen-reader passes on the main flows.
6. Local mode verified on Linux, Windows and macOS; server mode verified with the official Docker image.
7. Backup and restore of a project and of the instance state verified.
8. Upgrade path from the last beta verified.
9. No open `priority:p0` or `priority:p1` bugs.
10. Owner validation of every user-facing flow, recorded in the release issue.

## 5. Dependencies pinned for stability

- Apache Ossie is incubating (pre-1.0). Sextant pins the exact specification commit it supports and isolates it in the Ossie adapter; a newer specification is adopted through an issue and an ADR.
- DuckDB, DuckLake and every other dependency always track their **latest stable release** (ADR-0022), pinned exactly in lock files and updated through Dependabot pull requests that must pass the full CI gate.
- **Freeze during beta and release candidates:** only patch, minor and security updates are applied. A new major version released during beta or RC is applied right after `v1.0.0`, unless it fixes a security issue that affects Sextant.

## 6. Who approves each release

| Release | Approved by |
|---|---|
| Alphas (`v0.N.0-alpha.K`) and betas from `beta.2` | The Agent, when the exit checklist is fully evidenced; the Owner is notified |
| `beta.1`, every `rc`, `v1.0.0`, every stable patch | **Owner**, by commenting `Approved for release vX.Y.Z` on the release issue |
