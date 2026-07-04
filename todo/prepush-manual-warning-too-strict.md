# prepush-manual-warning blocks legitimate post-release pushes

## Problem

The `prepush-manual-warning` check in the pre-push hook exits non-zero on any manual push to the release branch (`main`), with the message "manual push to release branch (main) -- not via 'rlsbl release'". This forces every batch of post-release commits to go through a full `rlsbl release run` cycle just to reach origin.

In practice, after a release is cut (e.g., v0.4.0), there are legitimate commits that need to reach main: CI workflow fixes, documentation, infrastructure cleanup, dependency updates, test additions. These don't warrant a release — they're housekeeping. But the hook blocks them, forcing a patch release (v0.4.1) purely to satisfy the push mechanism. This conflates "pushing code" with "cutting a release."

## Current behavior

1. `rlsbl release run` pushes as part of the release flow — hook allows it (detects the release commit pattern)
2. Any other `git push origin main` — hook blocks with a warning that exits non-zero
3. No distinction between "I'm pushing unreleased work" (legitimate) and "I'm bypassing the release pipeline" (what the check is trying to prevent)

## What the check should protect against

The check exists to prevent accidental pushes that skip the release pipeline — e.g., someone pushing a version bump without running tests, changelog validation, or creating a GitHub Release. The danger is pushing a commit that SHOULD have been a release but wasn't.

## What it shouldn't block

Post-release maintenance commits that are explicitly NOT a release: CI fixes, documentation, refactors, test additions, dependency updates. These are between releases and will be included in the next release when it happens.

## Proposed fix

Allow pushes when HEAD is ahead of the last release tag AND no version-bump commit is in the push range. Specifically:

- Find the last release tag on the branch (`git describe --tags --match 'v*' --abbrev=0` or the releasable tag pattern)
- Check the commits being pushed (`$local_sha..$remote_sha` from the hook's stdin)
- If none of those commits match the release commit pattern (message = `vX.Y.Z` or contains a version bump in tracked files), allow the push
- If a commit DOES look like a release (version bump without going through `rlsbl release run`), block it

This preserves the safety guarantee (can't accidentally push a release commit) while allowing normal post-release development flow.

## Alternative: explicit opt-in flag

A simpler approach: `rlsbl push` command that pushes with the hook's blessing (sets an env var that the hook checks). This makes the intent explicit without complex heuristic detection. The hook blocks bare `git push` but `rlsbl push` is allowed for non-release pushes after running basic checks (clean tree, changelog coverage, tests pass).

## Impact

Currently affects any monorepo or project where the release branch is also the development branch (the common `main`-only pattern). Projects with separate `develop`→`main` flows are unaffected since pushes to `develop` aren't blocked.
