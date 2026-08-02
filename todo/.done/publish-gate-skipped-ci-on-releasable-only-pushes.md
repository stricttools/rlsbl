# Publish gate deadlocks when a release push touches only releasable metadata (skipped CI job)

## Context

In monorepo mode, the generated `ci-router.yml` uses a `dorny/paths-filter` step
to decide which project's CI jobs run on a push. The filters are built in
`rlsbl/commands/monorepo/sync.py` (`_generate_router`, filters block around
lines 118-130): each project's filter is `<project-path>/**` plus its `watch`
patterns. Releasable metadata under `.rlsbl-monorepo/releasables/<name>/**`
(changelog JSONL, version file, release files) is NOT included in any
project's filter.

The inline publish gate (`rlsbl/publish_gate.py`, consumed by
`rlsbl/commands/monorepo/publish_inline.py`, check-name matching via
`rlsbl/ci_router.py`) requires that every matching CI check run on the release
commit concluded `success`. It deliberately hard-fails on `skipped`.

## Problem

When a monorepo release push touches ONLY the releasable's changelog/release
files under `.rlsbl-monorepo/releasables/<name>/**` and nothing under the
project's own path or watch patterns, the router's paths-filter evaluates
false for that project, so its CI job is skipped on the release commit. The
publish gate then hard-refuses the skipped check.

This is inevitable for FIRST releases: the version write is a no-op (the file
already contains the initial version), so the release commit contains no
project-path change at all. The result is a deadlock with no sanctioned
recovery path:

- Re-running the CI workflow re-evaluates the same paths-filter on the same
  commit and skips again.
- `release retry` / re-dispatching the publish workflow re-evaluates
  identically.
- The only operational workaround is ensuring some project-path-touching
  commit rides the release push, which is not a documented or tool-mediated
  step.

## Verbatim gate error text

```
::error::Publish gate: CI did not pass on $sha -- refusing to publish.
```

followed by, for the skipped conclusion:

```
A CI check run matching the filter was SKIPPED. The gate cannot treat a skipped check as passing: this project's own CI must actually run on the release commit. Check paths filters and job conditions, re-run CI on this commit, then re-dispatch this publish workflow at the tag ref.
```

(`rlsbl/publish_gate.py`, lines 218 and 229.)

## Proposed structural fixes to evaluate

1. **Include `.rlsbl-monorepo/releasables/<name>/**` in each project's router
   path filter.** Rationale: releasable metadata IS a project change — a
   release commit is semantically a change to that project. Pros: smallest
   change (one line in `_generate_router`); CI runs on exactly the projects
   whose releasables changed; no gate changes. Cons: changelog-only commits
   (`rlsbl changelog add` auto-commits) would also trigger CI, costing some
   CI minutes on non-code pushes.

2. **Anchor the gate's CI check to the last project-path-touching commit
   at-or-before the release marker**, instead of strictly the release commit.
   Pros: no extra CI runs; gate verifies the code that is actually being
   published. Cons: gate logic gets materially more complex (must replicate
   the paths-filter semantics in the gate script to find that commit); weakens
   the simple invariant "CI passed on the tagged SHA".

3. **Force-run the released project's CI job on release pushes** (e.g., the
   router detects a release commit — message `^v\d+\.\d+\.\d+$` or a
   dispatch input — and overrides the filter for the released project).
   Pros: preserves the strict "CI on the exact release SHA" invariant. Cons:
   requires the router to know which project a release commit belongs to;
   more generated-workflow complexity than fix 1.

Fix 1 appears to be the most correct: it makes the class of skipped-on-release
failures impossible by construction, aligning the filter with the actual
definition of "this project changed".

## Affected files

- `rlsbl/commands/monorepo/sync.py` — router generation / filters block
- `rlsbl/publish_gate.py` — gate script (skipped handling, error text)
- `rlsbl/commands/monorepo/publish_inline.py` — gate consumer
- `rlsbl/ci_router.py` — job-key/check-name helpers
- `rlsbl/checks/workspace.py` — `workspace-ci-synced` check may need updating
  if filter contents change
- Tests covering router generation and the publish gate

## Effort estimate

- Fix 1: small — ~1-2 hours including a router-generation test asserting the
  releasable path appears in the filter, plus re-sync of generated workflows.
- Fix 2: medium-large — ~1-2 days; gate script rework plus careful testing of
  commit-anchoring edge cases.
- Fix 3: medium — ~0.5-1 day; router conditional logic plus tests.
