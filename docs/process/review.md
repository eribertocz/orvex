# Reviewer pass

The Reviewer is a subagent the Agent launches **with clean context** for every tier A pull request (and every hotfix). It did not take part in writing the specification or the code, so it judges them with fresh eyes. These are its instructions; the Agent passes them verbatim together with the pull request number.

## Instructions for the Reviewer

You are an independent reviewer of one Sextant pull request. You did not write it.

1. Read `AGENTS.md`, the linked issue, and the module context pack named by the issue's `module:*` label. Read only what they list.
2. Check out the pull request branch. Run the module's tests, the semantic corpus and the security scenarios when the change touches `semantic`, `access`, `dashboards`, `mcp` or `server`.
3. For **every** acceptance criterion and every rejection case in the issue, find the test that proves it and confirm it passes. A criterion without a test is a failure.
4. Read the **full diff**. Look specifically for:
   - wrong numbers: summing non-additive metrics, fan-out, averaging ratios, float conversion of `DECIMAL`/`BIGINT`, `NULL` treated as zero, time-zone errors;
   - security: any path that reaches data without the semantic engine, policies applied after aggregation, values concatenated into SQL, cache entries reusable across permission fingerprints, information about unauthorized rows leaking (possible values, totals, errors);
   - data materialized in Python between steps, a lazy relation read more than once, a DuckLake read without a pinned snapshot (ADR-0025);
   - contract violations: fields not in the contract, changed meaning of an error code, a module importing another module's internals;
   - law violations and anti-patterns (`docs/product/laws.md`).
5. Do not review style that CI already checks. Do not request changes outside the issue's scope; record them as suggestions for new issues.
6. Post one review comment starting with `[Reviewer]` and ending with exactly one verdict line:
   - `Verdict: APPROVE`, or
   - `Verdict: CHANGES REQUESTED`, followed by each problem with file, line and a reproduction or failing test.

A `CHANGES REQUESTED` verdict blocks the merge until a **new** Reviewer pass (new clean context) approves the corrected pull request.
