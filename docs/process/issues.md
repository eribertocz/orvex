# Issues: size, specification flow, readiness and completion

Designed to be **rigorous where errors are expensive and light everywhere else**: automated gates (conformance suites, types, CI) carry the quality; ceremony is kept to the minimum.

## 1. Hierarchy

```
Milestone = one roadmap phase (for example "P2 · Semantic engine parity · v0.3.0-alpha.1")
  └─ Issue = one deliverable = one pull request, labeled with its module
```

- There are no epic issues: the milestone groups a phase and the `module:*` label groups a module inside it.
- Every issue belongs to exactly one milestone and carries its roadmap ID (for example `P2-12`) in the body.
- A phase starts only after the previous phase's version is released (`roadmap.md`).

## 2. One standard size: medium

Every implementation issue is **medium**: neither trivially small nor too large to review.

| Criterion | A medium issue |
|---|---|
| Outcome | Delivers **one observable capability** with its tests (not a fragment that cannot be tested on its own) |
| Scope | One module (a contract file counts with the module that produces it) |
| Contracts | Creates **or** extends at most one contract, without breaking it |
| Size | About **300–800 changed lines**, excluding tests, fixtures and generated code |
| Review | Reviewable in about one hour |

Rules:

- **Too small** (well under 300 lines, or not testable on its own): merge it with the closest related issue of the same module.
- **Too large** (over 800 lines, more than one module or more than one contract): split it before implementation.
- **Endpoint exception:** an issue whose capability is reachable over HTTP also adds its route to `server` and its operation to the `http-api` contract with the generated client (`roadmap.md` §2). That thin route and the `http-api` extension do not count as a second module or a second contract.
- A **breaking** contract change is its own issue, with an ADR.
- Exceptions, never planned in the roadmap: bug fixes, hotfixes, dependency updates and documentation corrections may be smaller.
- If an issue turns out larger during implementation, the Agent stops, splits it, and continues with the first part.

## 3. Specifying ahead (the Agent specifies and implements)

The Agent alternates between two activities and **never mixes them in one step**:

1. **Phase start — outline and batch approval.** When a phase starts, the Agent creates the milestone and **all** issues of the phase with title, roadmap ID, module, goal and the list of new product behaviors. It opens one `type:decision` issue that summarizes the phase's new behaviors (with a Spanish summary) for the Owner, who approves the batch once.
2. **Rolling specification window.** The Agent always keeps the **next three** issues in the phase's order fully specified (`status:spec-ready`, §4) before starting to implement the first of them. Each time it merges a pull request, it specifies the next issue so the window stays at three.
3. **Frozen specification.** Once an issue is `status:spec-ready`, its acceptance criteria are not changed to fit the code. If the specification is wrong, the Agent comments on the issue explaining why, corrects it, and asks the Owner if the change alters approved product behavior.

Work in progress: at most **one issue being implemented** at a time; review passes and CI runs may overlap with specifying the next issues.

## 4. Definition of Ready (`status:spec-ready`)

1. **Goal** — one paragraph, observable outcome.
2. **Scope** and **out of scope**.
3. **Dependencies** — issue numbers, or "none".
4. **Contracts touched** — `contracts/<name>/vN`: new, extended or none.
5. **Acceptance criteria** — measurable, as a checklist, **including the cases that must be rejected** with their exact error codes.
6. **Test data** — fixtures, corpus cases or concrete values.
7. **Laws touched** — and how compliance is verified, or "none (purely technical)".
8. **References studied** — 2–4 tools (adopted / rejected / improved) when the issue introduces a new capability; otherwise "not applicable".
9. **Required reading** — the module pack and the exact sections; never "all docs".
10. **Risk tier** — A, B or C (`git-workflow.md` §3.5).
11. Labels, milestone and roadmap ID; link to the Owner's batch approval when the issue introduces new product behavior.

## 5. Definition of Done (required before merge)

1. Every acceptance criterion is met and evidenced in the pull request (case matrix).
2. Tests protect against regression; rejection cases assert the specified error codes.
3. All required checks for the current phase are green (`engineering.md` §8); the same commands pass locally.
4. Tier A: a `[Reviewer]` approval from an independent Reviewer pass.
5. Laws touched are documented in the pull request.
6. No `TODO`, dead code or commented-out code.
7. Public contracts changed are versioned; breaking changes have an ADR merged by the Owner.
8. User-facing text is internationalized (Spanish and English).
9. UI changes pass automated accessibility checks and include a keyboard walkthrough.
10. Performance-sensitive paths include before/after measurements.

## 6. Issue types

| Type label | Purpose |
|---|---|
| `type:feat`, `perf`, `refactor`, `test`, `build`, `ci`, `chore`, `security`, `docs` | A deliverable |
| `type:bug` | A defect against a specification, law or contract (fixed by a `fix(...)` pull request) |
| `type:decision` | A product question for the Owner, with options, recommendation and Spanish summary |
| `type:adr` | A decision to record in `docs/adr/` (only for laws, scope, public contracts or architecture) |
| `type:release` | Release checklist |

## 7. Labels and status automation

- **Manual labels:** exactly one `type:*` and one or more `module:*`. `priority:*` is optional (default `p1`); bugs always carry a priority. `owner-merge` marks pull requests touching governed paths.
- **Automatic status labels** (workflow created in P0-01): `status:in-progress` when a branch referencing the issue is pushed, `status:in-review` when its pull request is marked ready, removed when the issue closes.
- **Manual status labels:** `status:needs-spec` (outlined, not yet specified), `status:spec-ready`, `status:blocked` and `status:needs-owner-decision`, each with a comment explaining why.
