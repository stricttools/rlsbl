# Monorepo release: refresh a dev_node's editable sibling lock at bump time

## Context

A monorepo can contain a `dev_node = true` project (not itself releasable) that
depends on one or more releasable siblings through an **editable** uv path source,
so its `uv.lock` records the sibling's current version. A conformance/test-harness
node that installs the sibling implementations editable to run cross-implementation
tests is the canonical shape.

Such a node commonly ships a meta-test asserting its lock's recorded sibling
version equals the sibling's declared `pyproject.toml` version (this exists to
catch the lock drifting behind after a manual bump — a real recurring chore).

## Problem

When `rlsbl monorepo release run` bumps a releasable sibling (step 12 writes the
new version into the sibling's target files), it does **not** refresh the dev_node's
`uv.lock`. The lock still pins the pre-bump version, so:

1. The dev_node's own lock-pin meta-test fails on the release candidate.
2. That failure surfaces at the CI gate, red, after the untagged candidate is
   already pushed.

The fix-forward for it (`uv lock` in the dev_node dir) is honestly narrow — it
touches only the dev_node's `uv.lock`. But then:

3. That fix commit touches **only the dev_node's path**. None of the releasables'
   path-filtered CI jobs (e.g. `python/**`, `go/**`, `typescript/**`) match it, so
   on resume every releasable job reports `skipped`, and the publish gate refuses
   a skipped check.
4. The documented escape — dispatch the router workflow with a `run_all=true`
   input to short-circuit the path filters — requires that input to exist in the
   **remote** router workflow. A repo whose committed router predates the input
   must regenerate it via `monorepo sync`, but pushing the regenerated workflow
   requires a release push, which is exactly what the gate is refusing. Circular.

Net: a release that bumps a sibling a dev_node locks against can wedge itself, and
the only exits are (a) regenerate workflows and ride the resume-push to unlock the
forced dispatch, or (b) dishonestly widen the fix-forward window into a releasable's
tree so its CI triggers. Both are workarounds for a bump-time omission.

## Root cause

The version bump is the moment the lock goes stale, and it is the only moment the
release flow can refresh it as part of the candidate commit — so the candidate is
correct from the first push and CI is green the first time, with no dev_node-only
fix-forward and no circular re-trigger.

## Solutions

### A. Refresh dev_node editable-sibling locks during the bump (preferred)

At bump time, after writing new versions into a releasable's target files and
before pushing the candidate, detect every `dev_node` whose `uv.lock` records an
editable path source resolving to a just-bumped sibling, run `uv lock` in that
node, and include the refreshed lock in the version-bump commit.

- Pros: the candidate is self-consistent from the first push; the lock-pin
  meta-test passes on the first CI run; no dev_node-only fix-forward; no circular
  re-trigger; matches the "always commit uv.lock" policy.
- Cons: the release flow must discover editable path sources in dev_node locks
  (parse each dev_node `uv.lock` for `source = { editable = ... }` entries pointing
  at a workspace sibling) and shell out to `uv lock` per affected node; adds a uv
  invocation to the bump step.

### B. Make the CI gate treat a dev_node-only fix-forward as gate-relevant

Teach the gate that a change confined to a dev_node that runs the
cross-implementation suite still requires that suite's CI job, and wait on the
dev_node's own job rather than only the releasables' path-filtered jobs.

- Pros: unwedges the fix-forward path without changing the bump; the dev_node's CI
  (which runs the real conformance suite) is the honest thing to gate on for a
  conformance-lock change.
- Cons: does not prevent the stale candidate in the first place (CI still goes red
  once, the candidate is still pushed stale); only makes the recovery non-circular;
  the gate needs a notion of "dev_node job is gate-relevant for this change set".

### C. Both

A eliminates the first-time failure; B makes any residual dev_node-only fix-forward
recoverable without regenerating workflows. A is the fix; B is defense in depth.

## Affected files (indicative — verify against the tree)

- The monorepo release bump step (where target-file versions are written before the
  candidate push).
- The CI-gate job-selection / skipped-check logic.
- The `monorepo sync` router generation (only relevant if B's forced-dispatch path
  is kept as the documented recovery — ensure the `run_all` input is present in the
  generated router so an older committed router is not a trap).

## Effort

A: medium — lock discovery + a uv invocation folded into the bump, plus a
red-green integration test (bump a sibling a dev_node locks against; assert the
candidate commit carries the refreshed dev_node lock and the lock-pin meta-test
passes on the candidate). B: medium — gate logic plus a test that a dev_node-only
change is gated on the dev_node job. The recurring manual "refresh the conformance
lock" chore and the wedged-release recovery both disappear once A lands.
