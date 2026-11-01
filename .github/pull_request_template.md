Closes #<!-- n -->

## What changed
<!-- Plain description. -->

## Case matrix
<!-- Every acceptance criterion and every rejection case from the issue, with the test that covers it. -->
| Criterion / rejection case | Test | Result |
|---|---|---|
| | | |

## How it was verified
<!-- The summary printed by `tools/ci/verify` (full mode, clean checkout), plus exact commands and relevant output (not just "passed"). Name any check that runs only in CI and why. -->

## Size
<!-- Only when the PR changes more than about 400 lines (without generated and lock files): why it is not split (tests, fixtures, mechanical refactor, or technical coherence), and how to review it. Otherwise "under the guide". -->

## Time and CI-first errors
<!-- How long the issue took, and how many errors CI found before the local verification did (target: zero). -->

## Not done here
<!-- Improvements found but not needed by this issue, identified for separate handling, or "none". -->

## Laws touched
<!-- Which laws and how this PR complies, or "none (purely technical)". -->

## Contracts touched
<!-- contracts/<name>/vN: new | extended | breaking, or "none". -->

## Dependencies added
<!-- problem · package · version · license · governance · alternative · reason — or "none". -->

## Security (if applicable)
<!-- Threat model: what is protected, from whom, how it is tested. -->

## Risks

## Checklist
- [ ] The full local verification passed on a clean checkout of the commit
- [ ] Tests were written first and fail without the change; no existing test or criterion was lowered
- [ ] No decisions outside the issue's specification (any open question is on the issue)
- [ ] Tests protect this change from regression, including rejection cases
- [ ] No `TODO`, dead code or commented-out code
- [ ] No hard-coded product name; no secrets; no real company data
- [ ] User-facing text internationalized (es, en)
- [ ] UI: accessibility checks and keyboard walkthrough
