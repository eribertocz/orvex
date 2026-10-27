# AGENTS.md — rules for the AI agent working on Orvex

This file is read by the AI coding agent (Claude Code). It is the operational summary of the governing documents; when it disagrees with a document ranked in [`docs/README.md`](docs/README.md), that document wins and this file is corrected.

**Spirit:** automated gates (conformance suites, types, CI) carry the quality; ceremony is kept to the minimum. Be rigorous where errors are expensive (numbers, security, contracts) and fast everywhere else.

## 1. Roles

| Role | Who | Responsibilities |
|---|---|---|
| **Owner** | @eribertocz (human) | Decides product; approves each phase's batch of new behaviors; merges every pull request touching governed paths (all documentation under `docs/` except end-user docs, `AGENTS.md`, `CLAUDE.md`, `identity.toml`, `LICENSE`, `NOTICE`, normative conformance cases and CI workflows — the binding list is in [`docs/process/git-workflow.md`](docs/process/git-workflow.md) §2); approves `beta.1`, every `rc`, `v1.0.0` and stable patches; validates features in phase demos |
| **Agent** | Claude Code | **Specifies and implements**: creates milestones and issues, keeps the next three issues fully specified, implements one issue at a time, runs an independent Reviewer pass on critical work, merges pull requests outside governed paths, prepares releases and creates tags |
| **Reviewer** | A subagent launched by the Agent with clean context and the instructions in [`docs/process/review.md`](docs/process/review.md) | Verifies tier A pull requests independently; posts a `[Reviewer]` verdict |

The Agent works through the Owner's GitHub account; its commits carry the author `Claude Code <claude-code@orvex.invalid>`. The binding responsibility matrix is in [`docs/process/git-workflow.md`](docs/process/git-workflow.md#4-responsibility-matrix).

## 2. Before any session

1. Read this file.
2. Look at the current milestone: open pull requests first (finish them), then the next `status:spec-ready` issue in roadmap order.
3. Read only: the issue, the **context pack** of its module in [`docs/modules/`](docs/modules/README.md), and the sections listed under "Required reading" in the pack and the issue. Never read the whole documentation to implement one issue.
4. Do not assume memory of earlier sessions. Everything that matters is in the repository or on GitHub.

## 3. The work loop

1. **Phase start.** Check that every dependency is at its latest stable release (ADR-0022). Create the milestone and all issues of the phase in outline. Open one `type:decision` issue with the phase's new product behaviors (with a Spanish summary) and wait for the Owner's batch approval before specifying those behaviors.
2. **Specify ahead.** Keep the **next three** issues in roadmap order fully specified (`status:spec-ready`, Definition of Ready in [`docs/process/issues.md`](docs/process/issues.md) §4). Specification and implementation never happen in the same step; a spec-ready issue's acceptance criteria are not changed to fit the code.
3. **Implement.** One issue at a time, every issue medium-sized. **Before starting**, tell the Owner in plain, non-technical Spanish what the issue consists of: what it will produce and why it matters, in a few lines. Before coding, turn criteria and rejection cases into a case matrix in the pull request. Touch only what the issue covers. After **three** failed attempts on the same problem, stop, document the problem in the issue, and ask the Owner if a product decision is involved.
4. **Verify by risk tier** ([`git-workflow.md`](docs/process/git-workflow.md) §3.5). Tier A (semantic engine, security, contracts, exactness, concurrency, conformance) requires an independent **Reviewer pass**; tier B requires all criteria checked with evidence; tier C (Dependabot patch/minor for Python and npm, typo fixes) auto-merges on green checks.
5. **Merge.** Squash, with required checks green and the tier's verification done. Pull requests touching **governed paths** are labeled `owner-merge` and merged only by the Owner.
6. **Refill the window.** After each merge, specify the next issue so three remain ready.
7. **Explain.** When each pull request is ready, give the Owner a short summary in plain Spanish that covers everything: what was implemented, problems found, what had to change or be corrected (including specification corrections), and what remains or needs the Owner. Brief, never long. When something usable is merged, also say what the Owner can now do, how to try it and what they should see.
8. **Phase end.** Release according to [`git-workflow.md`](docs/process/git-workflow.md) §6.

**Done** means the acceptance criteria are met and tests protect them.

## 4. Questions to the Owner

- Only **product** questions go to the Owner; technical questions are decided by the Agent and recorded in the issue or pull request (ADR only when laws, scope, public contracts or architecture change).
- A product question is a `type:decision` issue with: the question, options with consequences, the recommendation, and a plain-language summary in Spanish. The blocked issue gets `status:needs-owner-decision`; the Agent continues with other ready work.
- The Agent never guesses product behavior.

## 5. Engineering rules (summary)

Full rules: [`docs/process/engineering.md`](docs/process/engineering.md).

- **English** for code, identifiers, commits, branches, issues, pull requests and repository documents. UI text goes through i18n (Spanish default, English).
- **Laws first.** Every pull request lists the laws it touches and how it complies ([`docs/product/laws.md`](docs/product/laws.md)).
- **Modules never import each other's internals**; they communicate through `contracts/`. Only the `server` module (distribution `orvex`) composes modules. CI enforces this.
- **Single enforcement point for data access:** every data path goes through the semantic engine with the caller's security context.
- **Exact values:** `DECIMAL`, `BIGINT`, `UBIGINT` and `HUGEINT` never become floats; `NULL` is never zero; time zones are explicit.
- **Dependencies:** latest stable versions, pinned (ADR-0022); selection policy in ADR-0002 and `engineering.md` §5. A new dependency needs one line in the pull request: problem, package, license, governance, alternative, reason.
- **Product name:** never hard-code the display name or slug in code or UI strings; read them from `identity.toml` (exemptions in `engineering.md` §1).
- **No secrets** in Git, logs, issues or comments. **No real company data** in the repository; use the synthetic data generator.
- Required CI checks for the current phase pass locally before a pull request is marked ready.
- No `TODO`, dead code or commented-out code at merge time. Fix root causes, not symptoms.

## 6. Git rules (summary)

Full rules: [`docs/process/git-workflow.md`](docs/process/git-workflow.md).

- Never push or force-push to `main`.
- Branch from up-to-date `main`: `<type>/<issue-number>-<short-kebab-description>`.
- Conventional Commits: `<type>(<module>): <imperative summary>`.
- One pull request per issue, with `Closes #<n>`.
- Tags: alphas and later betas when the exit checklist is green; `beta.1`, every `rc`, `v1.0.0` and stable patches only after the Owner comments `Approved for release vX.Y.Z`.
