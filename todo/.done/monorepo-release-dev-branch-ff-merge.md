# Monorepo batch release ignores `needs_ff_merge` (silent partial release from a dev branch)

## Context

`rlsbl` supports initiating a release from a non-release branch (a "dev branch"). When the current branch is not in `release_branches` (default `["main", "master"]`), the release flow is supposed to fast-forward merge the release branch up to the dev branch HEAD before tagging/pushing, so that the release commits actually land on the release branch. The dev branch must be a strict descendant of the release branch (no divergence).

The branch/remote preflight (`validate_branch_and_remote`) computes this correctly: when invoked from a dev branch it returns `needs_ff_merge=True`, and the **standalone** `rlsbl release run` path honors that signal — it fast-forward-merges the release branch to the dev HEAD and pushes the release branch.

The **monorepo batch** path does not.

## Problem

When `rlsbl monorepo release run` is invoked from a non-release (dev) branch:

- `validate_branch_and_remote` still returns `needs_ff_merge=True`.
- The batch release path ignores it entirely. It tags and pushes the **dev** branch and never fast-forward-merges the release branch.

### Consequence chain (why this is worse than a cosmetic bug)

1. Tags and pushes land on the dev branch, not the release branch.
2. The CI Router workflow triggers only on push to the release branch → CI never runs on the release commits.
3. Every Publish Router gate waits for the per-project CI check on the release commit. Because CI never ran on those commits, the gate check never appears.
4. Every gate times out. Tags and GitHub Releases get created, but **no registry publish happens**.

The net result is a **silent partial release**: from the operator's point of view the release "succeeded" (tags exist, GitHub Releases exist), but nothing was actually published to any registry. There is no hard error surfaced at release time — the failure only shows up later as timed-out Publish workflows.

## Reproduction shape

A dev-branch monorepo release:

1. A monorepo with one or more releasables and a CI Router / Publish Router CI setup (Publish gated on the release-commit CI check).
2. Check out a dev branch that is a strict descendant of the release branch (release branch is an ancestor; dev branch is ahead by the release-worthy commits).
3. Run `rlsbl monorepo release run --no-allow-dirty --watch --yes` from the dev branch.
4. Observe: tags/pushes go to the dev branch; the release branch is never fast-forwarded. CI Router never fires for the release commits; Publish Router gates time out; nothing publishes.

Contrast: the same setup released via the standalone `rlsbl release run` from a dev branch fast-forward-merges the release branch and publishes correctly.

## Expected behavior

The monorepo batch path must honor `needs_ff_merge` exactly as the standalone path does:

- When `validate_branch_and_remote` returns `needs_ff_merge=True`, fast-forward-merge the release branch to the dev branch HEAD and push the **release** branch (not the dev branch), mirroring the standalone handling.
- The strict-descendant guard should apply identically (refuse if the dev branch has diverged from the release branch).

## Affected files

- `rlsbl/commands/release/batch_release.py` — the monorepo batch release path that currently ignores the `needs_ff_merge` result.
- The standalone `rlsbl release run` path that already handles `needs_ff_merge` correctly — use it as the reference implementation for the fix (locate the call site of `validate_branch_and_remote` in the standalone flow and the ff-merge + release-branch-push logic it drives).

The likely correct fix is to extract the standalone path's ff-merge/push handling into a shared helper and call it from both paths, so the two cannot drift again.

## Solution options

### Option A — extract shared ff-merge/push helper, call from both paths (recommended)

Factor the standalone path's `needs_ff_merge` handling (ff-merge release branch to dev HEAD, push release branch, strict-descendant guard) into a shared helper used by both the standalone and monorepo batch flows.

- Pros: eliminates the drift permanently; a single code path means the two flows can't diverge again; matches the "collapse duplicated logic" preference.
- Cons: slightly larger refactor; needs care to keep the standalone path's existing behavior byte-for-byte.

### Option B — duplicate the `needs_ff_merge` handling into the batch path

Copy the ff-merge/push logic into `batch_release.py`.

- Pros: smaller, localized change.
- Cons: duplicates logic that already exists in the standalone path; the two can drift again (this bug is itself an instance of that drift).

### Option C — hard-error the batch path on `needs_ff_merge` until it is supported

Make the batch path refuse to release from a dev branch (clear error telling the operator to release from the release branch) rather than silently mis-releasing.

- Pros: trivial; immediately stops the silent partial release.
- Cons: removes a supported capability (dev-branch releases work in standalone); should at most be a stopgap, not the real fix. Prefer A.

Recommendation: Option A. It is the most correct solution and structurally prevents recurrence.

## Severity

High — silent partial release. Tags and GitHub Releases are created but no registry publish happens, and no error is surfaced at release time. Data/state on GitHub ends up inconsistent (tags/releases exist for versions that were never published), requiring manual cleanup or manual publish.

## Observed workaround

At each created tag ref, manually dispatch the CI Router workflow, wait for it to pass, then manually dispatch the Publish workflow. This is error-prone and defeats the point of the coordinated batch flow.

## Testing policy (red-green)

Per the ecosystem's red-green policy for bug fixes: write a regression test that reproduces a dev-branch monorepo release FIRST and verify it fails (asserting the release branch is fast-forwarded and the release branch — not the dev branch — is pushed, and that the batch path acts on `needs_ff_merge=True`). THEN apply the fix. THEN verify the test passes. Because this is an interaction between the batch flow and branch/remote validation, prefer an integration-style test exercising the full dev-branch batch release path, not just a unit test of `validate_branch_and_remote`.

## Effort estimate

Small–medium. The correct behavior already exists in the standalone path; the work is (1) a regression test for the dev-branch monorepo case, and (2) wiring the batch path to the same ff-merge/push handling (ideally via a shared helper per Option A). Estimate ~0.5–1 day including the integration test.
