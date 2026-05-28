# release retry should re-trigger CI, not just Publish

## Problem

`rlsbl release retry` re-creates the GitHub Release, which fires a `release:published` event. This re-triggers the Publish Router but NOT the CI Router (which triggers on `push` events). When a release has failing CI due to a bug fixed in later commits on main, there's no way to get green CI associated with that release without undoing and re-releasing.

Real-world case: strictcli v0.10.0 and go v0.6.0 shipped with a conformance test infrastructure bug (wrong subprocess CWD). The fix landed on main two commits later. The published packages are correct — only the CI conformance test step failed. But the GitHub Release shows red CI, and `retry` can't fix it because it only fires `release:published`, not `push`.

## Desired behavior

`rlsbl release retry` should be able to re-trigger CI workflows, not just publish workflows. Options:

1. **`gh workflow run` against HEAD** — re-trigger the CI Router workflow against the current HEAD (which has the fix). The CI run wouldn't be "associated" with the release tag's commit, but it would prove main is green.

2. **`gh workflow run` against the tag** — re-trigger CI against the tagged commit. Only works if the fix has been cherry-picked to the tag, which is unusual.

3. **Lightweight re-tag** — delete and re-create the tag pointing to the current HEAD (which includes the fix). Push the tag. This fires a push event with the new tag, re-triggering CI. Risky: changes what the tag points to.

4. **`retry --ci` flag** — explicitly re-trigger CI workflows via `gh workflow run` with the workflow filename and ref. This is the most flexible: `rlsbl release retry --ci` would call `gh workflow run ci-router.yml --ref main`.

## Recommendation

Option 4 (`--ci` flag) is cleanest. It's explicit, doesn't change tags, and uses the existing `gh workflow run` mechanism. The default `retry` (no flag) continues to only re-trigger publish. `retry --ci` additionally triggers CI workflows.

## Effort

Small. The `gh workflow run` call is straightforward. The main work is discovering the CI workflow filename(s) — either from config, convention, or the GitHub API.
