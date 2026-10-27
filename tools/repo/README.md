# Repository settings

The Owner applies these settings once, after the pull request of P0-01 is merged. They need repository-administrator rights, so the Agent prepares them but never applies them (`docs/process/git-workflow.md` §2 and §4).

## 1. Merge settings

Only squash merges; the squash commit takes the pull request title and keeps the commit messages (so `Co-Authored-By` trailers survive); head branches are deleted after merging; auto-merge is allowed for later Dependabot tier C updates.

```bash
gh api -X PATCH repos/eribertocz/sextant \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -f squash_merge_commit_title=PR_TITLE \
  -f squash_merge_commit_message=COMMIT_MESSAGES \
  -F delete_branch_on_merge=true \
  -F allow_auto_merge=true
```

## 2. Ruleset for `main`

[`main-ruleset.json`](main-ruleset.json) protects the default branch:

| Rule | Effect |
|---|---|
| `deletion`, `non_fast_forward` | `main` cannot be deleted or force-pushed |
| `required_linear_history` | No merge commits |
| `pull_request` | Every change goes through a pull request; 0 approvals (one shared account, ADR-0018); conversations resolved; squash only |
| `required_status_checks` | `governance` and `governance-scripts` must pass (later issues add their checks to this file) |
| `bypass_actors: []` | **Nobody bypasses the rules**, administrators included |

Create it the first time:

```bash
gh api -X POST repos/eribertocz/sextant/rulesets --input tools/repo/main-ruleset.json
```

Update it when a later issue adds a required check (the id comes from `gh api repos/eribertocz/sextant/rulesets`):

```bash
gh api -X PUT repos/eribertocz/sextant/rulesets/<id> --input tools/repo/main-ruleset.json
```

## 3. Verify

```bash
gh api repos/eribertocz/sextant --jq '{squash:.allow_squash_merge, merge:.allow_merge_commit, rebase:.allow_rebase_merge, delete_branch:.delete_branch_on_merge}'
gh api repos/eribertocz/sextant/rulesets --jq '.[] | {id, name, enforcement}'
```

The expected result is squash `true`, merge `false`, rebase `false`, delete_branch `true`, and one active ruleset named `main`.
