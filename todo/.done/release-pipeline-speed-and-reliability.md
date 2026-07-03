# Release pipeline speed and reliability improvements

## 1. Skip preflight tests during release (duplicate work)

The release pipeline runs `go test ./... -race -short -count=1` locally via the `test-suite` preflight check, then pushes, and CI runs the same tests again. There is no `--skip-preflight` flag. For projects with fast CI, skipping local tests saves 30-120s per release. The local test run is purely defensive duplication — if CI catches the failure, the release can be reverted.

## 2. Make CI watch async by default

`--watch` blocks the terminal for the entire CI duration (2-10 minutes). The release is functionally complete after push + GitHub Release creation. The watch should either: (a) default to printing the watch command and exiting, with `--watch` opting into blocking mode, or (b) spawn a background process that sends a desktop notification when CI completes.

## 3. Auto-clear stale in-progress state

When all steps in `completed_steps` (VERSION_BUMPED, COMMITTED, CHANGELOG_FINALIZED, RELEASE_FILE_FINALIZED, TAGGED, PUSHED, GITHUB_RELEASE) are present in `in-progress.json`, the release actually completed — the process was just killed before `clear_release_state()` ran. Currently this requires `rlsbl release resume` which re-runs post-release steps unnecessarily. The fix: if all expected steps are completed, auto-clear the state file and print a message.

## 4. Fix watch late-poll retry deduplication

`_watch_runs()` in `watch.py` has a race condition. After initial runs complete, it re-polls for late-starting workflows. Retry runs (dispatched via `gh workflow run`) share the same commit SHA as the original, so the late-poll treats them as new late-starting workflows and retries them again. The `retried_workflows` set is scoped per `_watch_runs` call — the late-poll creates a fresh call with a fresh set, so the dedup doesn't work across the two invocations. Fix: carry the `retried_workflows` set from the initial `_watch_runs` into the late-poll `_watch_runs` call, or track all dispatched retry run IDs and exclude them from late-poll results.

## 5. Prevent duplicate Publish workflow runs

The Publish workflow template has both `release: [published]` and `workflow_dispatch` triggers. When a release creates a GitHub Release (triggering `on: release`), and then rlsbl watch retries the workflow (triggering `workflow_dispatch`), both run concurrently. The dispatch run fails because goreleaser tries to upload assets that already exist (HTTP 422 `already_exists`). Fix: add a concurrency group to the Publish workflow template (`concurrency: { group: publish-${{ github.ref }}, cancel-in-progress: false }`) so duplicate runs queue instead of running in parallel and failing.
