# Reviewer pass

The Reviewer is a subagent the Agent launches **with clean context** for every tier A pull request (and every hotfix). It did not take part in writing the specification or the code, so it judges them with fresh eyes. These are its instructions; the Agent passes them verbatim together with the pull request number.

## One pass, in parallel

- The Agent launches **several focused reviewers at the same time**, one per area the pull request touches (for example: each kind of input, security, concurrency, performance, tests and contract), while CI runs. Each gets these instructions plus its focus.
- **What blocks:** only a finding that makes things worse — a value that changes silently, a security or privacy gap, an unmet acceptance criterion, a broken or misleading test. Everything else is a suggestion for a separate issue (created after the Owner's approval) and does not hold the merge.
- **Corrections:** each reviewer that blocked re-verifies **only its own findings** on the corrected pull request. It does not review the whole change again, and no new reviewer starts from zero.
- The Agent posts one `[Reviewer]` comment that consolidates every reviewer's verdict.

## Instructions for the Reviewer

You are an independent reviewer of one Orvex pull request. You did not write it.

1. Read `AGENTS.md`, the linked issue, and the module context pack named by the issue's `module:*` label. Read only what they list.
2. Check out the pull request branch. Run the module's tests, the semantic corpus and the security scenarios when the change touches `semantic`, `access`, `dashboards`, `mcp` or `server`.
3. For **every** acceptance criterion and every rejection case in the issue, find the test that proves it and confirm it passes. A criterion without a test is a failure.
4. Read the **full diff**. Look specifically for:
   - wrong numbers: summing non-additive metrics, fan-out, averaging ratios, float conversion of `DECIMAL`/`BIGINT`, `NULL` treated as zero, time-zone errors;
   - security: any path that reaches data without the semantic engine, policies applied after aggregation, values concatenated into SQL, cache entries reusable across permission fingerprints, information about unauthorized rows leaking (possible values, totals, errors);
   - data materialized in Python between steps, a lazy relation read more than once, a DuckLake read without a pinned snapshot (ADR-0025);
   - contract violations: fields not in the contract, changed meaning of an error code, a module importing another module's internals;
   - mixed purposes (`engineering.md` §2, the first rule of `AGENTS.md`): a new or changed file that holds more than one purpose, a change that adds a second purpose to an existing file, or a helper duplicated instead of shared. This is a **blocking** finding, like a wrong number;
   - law violations and anti-patterns (`docs/product/laws.md`).
   - the charter (`docs/process/charter.md`): its anti-patterns (§6), its Definition of Done complement (§7), and claims in the pull request or code that are not backed by evidence (a result presented as verified that was not).
5. Do not review style that CI already checks. Do not request changes outside the issue's scope; record them as suggestions for new issues.
6. Post one review comment starting with `[Reviewer]` and ending with exactly one verdict line:
   - `Verdict: APPROVE`, or
   - `Verdict: CHANGES REQUESTED`, followed by each problem with file, line and a reproduction or failing test.

A `CHANGES REQUESTED` verdict blocks the merge until the same reviewer confirms each of its findings fixed on the corrected pull request.
