---
name: Release
about: Release checklist. Opened by the Agent; approved by the Owner when required (versioning.md §6).
title: "release: vX.Y.Z[-pre]"
labels: ["type:release"]
---

**Version:** <!-- e.g. v0.3.0-alpha.1 --> · **Phase:** <!-- P2 --> · **Milestone:** <!-- link -->

## Exit criteria (from docs/roadmap.md)
- [ ] 

## Evidence
<!-- Links to CI runs, conformance reports, measurements, Owner validation comments. -->

## Module versions (versions.toml)
| Module | Version |
|---|---|
| | |

## Steps (docs/process/git-workflow.md §6)
- [ ] Release pull request opened and merged by the Agent
- [ ] Owner comment `Approved for release vX.Y.Z` (only for `beta.1`, every `rc`, `v1.0.0` and stable patches)
- [ ] Module tags and product tag created by the Agent
- [ ] GitHub release published; artifacts built by CI
- [ ] Closing summary: ✅ Done · 🔍 Verification · ➡️ Next
