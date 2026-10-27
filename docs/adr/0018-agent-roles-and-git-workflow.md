# 0018 — Agent roles, GitHub identity and Git workflow

**Status:** Accepted (2026-10-26)

## Context
Orvex is specified and implemented by an AI agent. bi-platform used two agents with strict roles, but both shared one GitHub account, so formal approvals were impossible. A second account (a machine account or a GitHub App) adds setup and maintenance the Owner considers too complex. What matters is that quality does not depend on an agent approving itself.

## Decision
- **Roles:** Owner (human), **Agent** (Claude Code: specifies **and** implements) and **Reviewer** (a subagent launched by the Agent with clean context and the instructions in `process/review.md`). Rules live in `AGENTS.md`.
- **One account:** the Agent works through the Owner's GitHub account; its commits carry the author `Claude Code <claude-code@orvex.invalid>` so its work stays distinguishable.
- **Specify ahead:** the Agent keeps the next three issues fully specified; a specification is frozen before its implementation branch exists and is never changed to fit the code.
- **Control without a second account:**
  1. required automated checks (types, tests, semantic corpus, security scenarios) block merging;
  2. tier A pull requests require an independent Reviewer pass;
  3. pull requests touching **governed paths** (laws, ADRs, normative cases, CI workflows, licenses) carry `owner-merge` and are merged only by the Owner; the `governance` check enforces the label;
  4. the Owner approves each phase's batch of new behaviors and the stable-track releases.
- Coordination only through GitHub; product questions go to the Owner as `type:decision` issues with a recommendation and a Spanish summary.
- Branch protection, Conventional Commits, squash merges, the responsibility matrix and the release procedure are defined in `process/git-workflow.md`.

## Alternatives considered
- Two agents (Specifier and Implementer) with a machine account for approvals: real separation, but more accounts, credentials and coordination; rejected as too complex for one Owner.
- GitHub App for approvals: same separation with more setup; possible later without changing the process.
- One agent without independent review: the agent would grade its own work; rejected.

## Consequences
GitHub cannot technically prevent the Agent from merging, so governed-path protection relies on the `governance` check plus branch protection requiring it. Independence comes from the Reviewer's clean context and from the conformance suites, not from separate accounts.
