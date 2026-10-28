# Engineering standards

Applies to every pull request. These are default acceptance criteria of every issue; the Agent meets them without restating them in each issue.

## 1. Language and naming

- English for all identifiers, comments, commit messages, documents, issues and pull requests.
- Python: `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants. Names describe domain intent (`compile_semantic_query`, `PermissionFingerprint`), never implementation trivia (`helper2`, `data_list`).
- TypeScript: `camelCase` for variables and functions, `PascalCase` for types and Svelte components.
- No abbreviations except widely established ones (`id`, `sql`, `url`, `api`).
- **Product name rule.** The display name and slug are read at runtime from `identity.toml` wherever they appear in behavior or UI (window titles, CLI help, the instance directory `~/.<slug>/`, the project directory `.<slug>/`, environment-variable prefix `<SLUG>_`, Ossie `vendor_name`, Docker image name). The CI check (P0-04) fails on hard-coded occurrences in source code and UI strings, **except**:
  - Python namespace directories and distribution names (`orvex/`, `orvex-*`), which the rename script rewrites;
  - documentation (`*.md`), `NOTICE`, `.github/labels.yml`, `.github/ISSUE_TEMPLATE/`;
  - the Agent's commit identity (`claude-code@orvex.invalid`) and test fixtures that assert the identity loader.
  The rename script updates every exempt location in one commit.

## 2. Architecture inside a module

Every module has three layers. Dependencies point inward only.

| Layer | Contains | May depend on |
|---|---|---|
| `domain` | Pure logic and immutable types. No I/O, no clock, no randomness, no global state | Standard library and its own contract types only |
| `application` | Use cases that orchestrate the domain | `domain` |
| `adapters` | DuckDB, HTTP, files, CLI, external services | `application`, `domain` and the external library it adapts |

Rules:

- **Explicit over abstract.** An abstraction (protocol, interface) exists only at a contract boundary or where a second implementation is real. No speculative layers, no dependency-injection frameworks; dependencies are passed explicitly through constructors.
- Domain types are immutable (`dataclass(frozen=True, slots=True)` or equivalent).
- No module imports another module's internals. Cross-module communication uses contract types only. CI enforces this with an import-boundary check written with the standard library (`ast`).
- Errors are explicit, typed per module, and carry a stable `code` (for example `AMBIGUOUS_RELATIONSHIP_PATH`), a human message and structured details. No bare `except`, no swallowed exceptions.
- **Data stays in DuckDB (ADR-0025).** Rows never pass through Python between steps: compose SQL or lazy DuckDB relations, write with `INSERT … SELECT`, `MERGE`, `CREATE TABLE … AS` or `COPY`, and read each final result **exactly once** into Arrow. A lazy relation reruns its query on every read, so a second read, `str(rel)` or `fetchall` on it is a defect. Every DuckLake read pins its snapshot. `DuckDBPyRelation` never crosses a module boundary; contracts exchange Arrow.
- SQL sent to DuckDB is generated from a plan or written as plain SQL with bound parameters. **Never** concatenate user input into SQL. Session variables (`SET VARIABLE`) are never used on shared connections; values are passed as explicit prepared-statement parameters.

## 3. Typing

- Python: `mypy --strict` passes with no ignores except documented ones in third-party stubs.
- TypeScript: `strict: true`; `svelte-check` passes with no warnings.
- Contract types are generated from `contracts/` (JSON Schema, OpenAPI); hand-written duplicates are prohibited.

## 4. Testing

| Level | Purpose | Required when |
|---|---|---|
| Unit | Domain logic | Always for domain code |
| Contract | A module honors its contract (inputs, outputs, errors) | Any contract is touched |
| Conformance | Semantic corpus and security suite | Semantic engine or security is touched |
| Property-based | Invariants over generated inputs | Parsers, compilers, planners, security, numeric code |
| Integration | Real DuckDB, DuckLake, PostgreSQL, ClickHouse | Adapters are touched |
| End-to-end | Browser flows | UI flows are touched |
| Accessibility | Automated checks plus keyboard walkthrough | UI is touched |
| Performance | Before/after measurement on the reference dataset | Hot paths are touched |

Rules:

- Tests are deterministic and reproducible: the same commands locally and in CI.
- Every rejection the specification defines is tested and asserts the exact error code.
- No mocks of DuckDB in integration tests; use real in-process DuckDB.
- Exactness tests compare against the oracle (expected CSV) value by value, including `NULL` and decimal scale.

## 5. Dependencies

Selection order (ADR-0002):

1. Python or browser standard library.
2. Capabilities of DuckDB itself.
3. Projects governed by a foundation (Apache, Linux Foundation, CNCF, Eclipse, Python Software Foundation) or a large organization.
4. Nothing that depends on a single maintainer at runtime.

Development-only tools (linters, formatters, test runners) may come from smaller projects because they are not shipped; they must be replaceable without touching product code.

Runtime libraries maintained by a funded team of at least three active maintainers (for example Svelte Flow by xyflow) are acceptable at rank 3. Single-maintainer runtime libraries are allowed only as isolated exceptions recorded in ADR-0002 (currently Gridstack). Libraries for Excel and PDF export are chosen in their issues (P6-18, P6-19, P7-11) under this policy; a single-maintainer library there requires an ADR.

Every new dependency requires one line in the pull request: problem, package, version, license, governance/maintainers, alternative considered, reason. Licenses must be permissive. Runtime dependencies may use Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, ISC, 0BSD or PSF-2.0; development tools may also use MPL-2.0, MIT-0, BlueOak-1.0.0, CC0-1.0 or Python-2.0. The binding list is `tools/licenses/allowed.toml`, enforced by the `licenses` CI check; changing it needs this section updated in the same pull request. Per-package exceptions, each with its reason and issue: `certifi` (MPL-2.0; Mozilla's CA certificate bundle, a required dependency of the official ClickHouse client, used unmodified; #65).

## 6. Security

- Authorization is decided on the server; the client is never trusted.
- Secrets come from environment variables or a secret store; never from files in Git, logs, issues or comments.
- Pull requests touching security include a short threat model: what is protected, from whom, how it is tested.
- Logs never contain secrets, personal data or query result values.

## 7. Performance

- Measure before optimizing; record before/after numbers in the pull request.
- No blocking DuckDB calls inside the async event loop; they run in worker threads.
- Memory and thread limits of DuckDB are configured explicitly.

## 8. Required CI checks by phase

A check becomes **required** (blocks merging) from the issue that creates it; from then on it stays required.

| Check | Command family | Created by | Required from |
|---|---|---|---|
| Governance | pull requests touching governed paths carry `owner-merge` | P0-01 | P0 |
| Python format and lint | `ruff format --check`, `ruff check` (development-only tools) | P0-02 | P0 |
| Python types | `mypy --strict` | P0-02 | P0 |
| Python tests | `pytest` per module | P0-02 | P0 |
| Web checks | `svelte-check`, lint, unit tests, build | P0-03 | P0 |
| Licenses | dependency license allow-list | P0-03 | P0 |
| Import boundaries and materialization | standard-library `ast` checker: cross-module imports; `fetchall`/`fetchone`/`fetchmany`/`.df()`/`.pl()`/`to_pandas` outside the allow-list (ADR-0025) | P0-04 | P0 |
| Product-name check | §1 rule | P0-04 | P0 |
| Contracts | JSON Schema and OpenAPI validation; generated types current | P0-05 | P0 |
| Semantic corpus | corpus runner, reporting per case | P0-11 | Reported from P0; **gate 21/21 from P2-27** |
| Security conformance | scenarios runner | P3-13 | P3 |
| End-to-end | browser tests on main flows, both runtime modes on Linux | P6-21 | P6 |
| Accessibility | automated checks on every route | P6-21 | P6 |
| Operating-system matrix | local mode on Linux, Windows, macOS | P9-10 | P9 |

Exact commands are fixed by the issue that creates each check and listed in each module's `README.md`.

## 9. Versions of runtimes and libraries (ADR-0022)

- **Always the latest stable release** of every runtime, library and tool; never alpha, beta, release candidates or nightly builds in `main`.
- Python: the latest stable release; CI also runs the previous stable minor release so users on it keep working.
- Node.js: the latest LTS release, pinned in `.nvmrc`.
- Versions are pinned exactly in lock files: Python with the standard lock-file format `pylock.toml` (PEP 751), web with `package-lock.json`. If the chosen installer cannot produce `pylock.toml` when P0-02 starts, the Agent records the standards-based alternative in P0-02. Dependabot opens weekly update pull requests (security updates immediately), which merge only with the required checks green.
- PostgreSQL (server mode and tests): latest stable major version.
- At the start of each phase the Agent checks that every dependency is on its latest stable release and opens update issues for any gap.

## 10. Documentation

- Write documentation only when code, contracts and tests cannot express the information.
- Every module has a `README.md`: purpose, contract, how to run its tests.
- Decisions go to ADRs, not to code comments.
