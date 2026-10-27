# Git and GitHub workflow

Binding for the Owner and the Agent. Everything is in English.

## 1. Roles and identity

| Role | Who | GitHub identity |
|---|---|---|
| **Owner** | @eribertocz (human) | Personal account `eribertocz` |
| **Agent** | One AI coding agent (Claude Code) that **both specifies and implements** | Works through the Owner's account; every commit it creates has the author `Claude Code <claude-code@orvex.invalid>` so the history shows what the Agent wrote |

There are no other accounts, machine users or GitHub Apps. Because the Agent and the Owner share one GitHub account, GitHub approvals cannot distinguish them; control is therefore defined by **rules on who merges what** (§4) and by **automated gates** (required checks).

## 2. Branch protection on `main` (configured in P0-01)

The Agent prepares the exact settings in P0-01 (as a checklist and a script using the GitHub API); the **Owner applies them**, because they need repository-administrator rights.

- Changes only through pull requests; no direct pushes; no force pushes; no deletion.
- **Do not allow bypassing** the rules, including for administrators: the Owner's account, which the Agent also uses, is bound by them.
- Required status checks: those defined for the current phase (`engineering.md` §8).
- No required review count (the Agent cannot be reviewed by a second GitHub identity); independent review is done by the Reviewer pass (§3.5).
- Linear history; merge method **squash only**; conversations resolved before merging.

### Governed paths

Pull requests that touch any of these paths are **merged only by the Owner**. The Agent prepares them, labels them `owner-merge`, and asks for review. A required CI check (`governance`, created in P0-01) fails while a pull request touching governed paths lacks the `owner-merge` label, so such pull requests are always visible to the Owner. The check runs as defined on the **base branch** at the time it runs (workflow, script and the block below), so a pull request cannot weaken its own check.

Format (machine-read): one pattern per line; the first token is the pattern, the rest of the line is a comment. A pattern ending in `/` matches every path under it; any other pattern matches one file exactly. A leading `!` marks an exclusion, which wins over inclusions.

```
docs/
!docs/user/      end-user documentation is not governed
AGENTS.md
CLAUDE.md
identity.toml
LICENSE
NOTICE
conformance/semantic-corpus/
conformance/security/scenarios/
.github/workflows/
```

## 3. Branches, commits and pull requests

### 3.1 Branches

- Created from up-to-date `main`.
- Name: `<type>/<issue-number>-<short-kebab-description>`, for example `feat/42-semantic-query-contract`.
- Allowed `<type>`: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`, `chore`, `release`, `hotfix`.
- One branch per issue; deleted automatically after merge.

### 3.2 Commits

- [Conventional Commits 1.0](https://www.conventionalcommits.org/): `<type>(<scope>): <imperative summary>`.
- `<scope>`: `store`, `transform`, `semantic`, `access`, `dashboards`, `mcp`, `server`, `web`, `contracts`, `conformance` or `repo`.
- Breaking changes add `!` after the scope and a `BREAKING CHANGE:` footer.
- Agent commits use the author `Claude Code <claude-code@orvex.invalid>`; the Owner's commits use the Owner's identity.

### 3.3 Pull requests

- One pull request per issue, with `Closes #<n>`.
- The description follows the template (case matrix, verification commands and output, laws, contracts, dependencies, risks). Sections that do not apply say "none".
- The pull request stays within the issue's medium size; if the work grows beyond it, the Agent stops and splits the issue before continuing (`issues.md` §2).
- Drafts allowed while working; **Ready for review** only when local checks pass.

### 3.4 Specification before code

The Agent specifies and implements, but **never in the same step**: the specification is written and frozen in the issue before the implementation branch is created. During implementation the Agent may not change acceptance criteria to fit the code. If the specification proves wrong, the Agent stops, corrects the issue with a comment explaining why, and — if the change alters approved product behavior — asks the Owner.

### 3.5 Reviewer pass (by risk tier)

Before merging, the Agent verifies its own pull request according to the risk tier:

| Tier | Applies to | Verification |
|---|---|---|
| **A — critical** | `semantic`, `access`, security, contracts, numeric exactness, concurrency, `conformance/` | All required checks green **and** an independent **Reviewer pass**: a separate subagent session with clean context and reviewer instructions (`docs/process/review.md`) that did not take part in writing the code. It checks every acceptance criterion and reads the full diff. Its verdict is posted on the pull request as a comment starting with `[Reviewer]` |
| **B — standard** | `web`, `dashboards`, `transform`, `store`, `server`, `mcp`, tooling (unless they touch tier A topics) | All required checks green; the Agent checks every criterion and posts the case matrix with results |
| **C — routine** | Dependabot patch and minor updates of Python and npm packages; typo fixes outside governed paths | Auto-merge when all required checks are green. GitHub Actions updates touch `.github/workflows/` and are therefore `owner-merge` |

A `[Reviewer]` verdict of **changes requested** blocks the merge until a new Reviewer pass approves.

### 3.6 Merging

- The Agent merges (squash) its own pull requests when: required checks are green, the tier's verification is complete, and the pull request does **not** touch governed paths.
- The Owner merges every pull request labeled `owner-merge`.
- The issue closes automatically through `Closes #<n>`.
- A Dependabot pull request that fails CI becomes a `type:chore` issue for the Agent.

## 4. Responsibility matrix

**R** = does it · **A** = approves / accountable · **I** = informed.

| Activity | Owner | Agent |
|---|---|---|
| Decide product behavior and scope | **A/R** | I (proposes) |
| Approve the phase's batch of new behaviors | **A** | R (presents) |
| Write or change laws, ADRs, normative conformance cases | **A** (merges) | R (prepares) |
| Create milestones and issues; keep three issues specified ahead | I | **R** |
| Branch, commit, push, open pull requests | — | **R** |
| Reviewer pass on tier A pull requests | I | **R** (independent subagent) |
| Merge pull requests outside governed paths | I | **R** |
| Merge pull requests touching governed paths (`owner-merge`) | **R** | prepares |
| Explain a merged usable feature in plain Spanish | I | **R** |
| Validate usable features (phase demos) | **A/R** | I |
| Prepare releases (release issue, release pull request) | I | **R** |
| Approve `beta.1`, every `rc`, `v1.0.0` and stable patches | **A/R** | I |
| Create tags and GitHub releases | I | **R** (after approval when required) |
| Configure repository settings and branch protection | **R** (applies) | prepares |

## 5. Labels

Defined in `.github/labels.yml`; usage rules in [`issues.md`](issues.md) §7.

## 6. Tags and releases

Version rules: [`versioning.md`](versioning.md).

1. The Agent opens the release issue `release: vX.Y.Z[-pre]` with the phase exit checklist (`roadmap.md`) and evidence links.
2. The Agent opens the release pull request (`release/<issue>-vX.Y.Z`): module version bumps, changelog from Conventional Commits, `versions.toml`.
3. Approval:
   - **alphas and betas from `beta.2`:** the Agent proceeds when every checklist item is evidenced, and notifies the Owner in the release issue;
   - **`beta.1`, every `rc`, `v1.0.0` and stable patches:** the Agent waits until the Owner comments `Approved for release vX.Y.Z` on the release issue.
4. The Agent merges the release pull request (it touches no governed paths), creates annotated tags (module tags and the product tag) on the merge commit and pushes them. The `release` workflow builds the artifacts from the product tag, checks that the tag equals the product version in `versions.toml`, and publishes the GitHub release.
5. The Agent closes the release issue with: `✅ Done · 🔍 Verification · ➡️ Next`.

## 7. Hotfixes

For a critical defect in a released version (wrong numbers, data leak, crash on the main flow):

- If `main` contains nothing unreleased that would block a release, branch `hotfix/<issue>-<description>` from `main`, fix, and release the next patch version (`vX.Y.(Z+1)`) through §6.
- Otherwise, create `release/vX.Y` from the affected tag, fix there through a pull request into `release/vX.Y`, tag `vX.Y.(Z+1)` from that branch, and port the fix to `main` with a second pull request.
- A Reviewer pass is mandatory for every hotfix; the Owner approves the patch release.
