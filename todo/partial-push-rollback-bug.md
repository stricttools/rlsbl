# Bug: local rollback after partial push creates divergent state

## What happened

During `rlsbl release run` for shopkeep v0.4.2:

1. rlsbl committed the version bump + finalized changelog
2. rlsbl pushed the commits to remote — this succeeded
3. rlsbl tried to push the tag — this failed (pre-push hook ran the full test suite, likely a timeout or transient error)
4. rlsbl saw the failure and rolled back locally with `git reset` to the pre-release commit
5. The remote now has the release commits but local doesn't — divergent state
6. No tag exists anywhere (local or remote)
7. `git pull --rebase` was needed to re-sync, then manual `git tag` + `git push origin <tag>` + `gh release create` to finish the release

## The bug

rlsbl should not roll back commits that were already pushed to the remote. Once `git push origin main` succeeds, the commits are public. A local `git reset` after that creates a local/remote divergence that requires manual intervention.

## Expected behavior

If commits pushed but the tag push failed:
- Do NOT roll back the commits locally
- Retry the tag push (1-2 attempts)
- If tag push still fails, print a clear message: "Commits pushed but tag failed. Run: git tag vX.Y.Z && git push origin vX.Y.Z"
- Leave local state in sync with remote

## Reproduction

Hard to reproduce reliably — depends on the pre-push hook timing out or a transient network error during the tag push step. The shopkeep project's pre-push hook runs the full pytest suite (727 tests, ~12 seconds) which may contribute to timeouts.

## Impact

The user had to manually create the tag and GitHub Release. The release was otherwise correct — version, changelog, and code were all fine. Just the tag and release were missing.
