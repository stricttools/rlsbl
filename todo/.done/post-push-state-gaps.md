# Post-push failure state management gaps

## Context

The v0.104.1 fix (`_handle_resumable_push_failure` in execute.py:778-839)
correctly classifies post-TAGGED push failures as resumable: it writes
in-progress.json with a failed PUSHED marker and re-raises. The fix covers
all exception types (`except Exception`) and has explicit test coverage for
`CalledProcessError(1, "git push")`.

Despite this, three incidents across orxtra releases (v0.10.0, v0.10.1,
v0.11.0) ended with "everything done, push not done, no resumable state."
Investigation traced the root cause to three peripheral gaps around the fix,
not the fix itself.

## Bug 1 — `run_cmd` does not catch `CalledProcessError`

`run_cmd` (commands/release/__init__.py) catches `PostReleaseError`,
`ReleaseValidationError`, `HookError`, and `ConfigError`, converting each
to `sys.exit(1)`. A raw `CalledProcessError` from a failed push propagates
as an unhandled exception with a Python traceback.

Consequence: the inner `_handle_resumable_push_failure` writes the state
file and prints "Fix the issue and resume", but the subsequent unhandled
traceback buries that message. In batch mode (`batch_release.py`), the
batch loop only catches `SystemExit` — a `CalledProcessError` escapes the
loop entirely, skipping `_archive_batch_if_complete` and producing a raw
crash.

Fix: add a catch-all `except Exception` at the end of `run_cmd` that
prints the error cleanly and calls `sys.exit(1)`. The batch loop's
`except SystemExit` then catches all failure modes uniformly, and the
resume guidance is the last thing the user sees.

## Bug 2 — Retry-success treated as permanent failure

`_handle_resumable_push_failure` retries the tag push up to 2 times when
the branch push succeeded but the tag push failed (execute.py:799-810).
When a retry SUCCEEDS, it marks PUSHED as complete via `save_step` — but
then falls through to print "Error: push failed" and the outer code still
re-raises the original exception.

Consequence: a transient failure that clears on retry is reported as a
hard failure. All post-push steps (GITHUB_RELEASE, ASSETS_UPLOADED,
PIPELINES_PUBLISHED, DEPLOYED, POST_HOOKS_RUN, SNAPSHOT_REGENERATED) are
skipped despite the push having succeeded. The user must run
`release resume` to pick up the post-push steps even though the push
is done.

Fix: when the retry succeeds and PUSHED is marked complete, return early
from `_handle_resumable_push_failure` instead of falling through to the
error message. The outer code should check whether PUSHED ended up in
`_completed` after the handler returns and skip the re-raise if so,
continuing into the post-push steps normally.

## Bug 3 — Batch repair pass archives on local tag presence (not remote)

`plan_all_released` (batch_plan.py:307-312) checks whether each batch
item is "released" by verifying the LOCAL tag exists and the live version
matches the plan's target. It does NOT verify the tag exists on the
remote.

After a push failure, the local tag exists (it was created in the TAGGED
step before the push). On the next batch command invocation, the repair
pass at `_cmd_batch_release` (batch_release.py:186-193) calls
`plan_all_released`, finds everything "released" by local evidence, and
archives the batch file. But the push never landed — the tag is local
only.

This creates the observed state: the batch file is archived (looks
complete), but the remote has none of the release commits or tags.
`release resume` may still work (in-progress.json was preserved by the
fix), but the archived batch file makes a fresh `monorepo release run`
think there is nothing to release.

Fix: add a `tag_exists_on_remote` check to `item_is_released` so the
repair pass does not prematurely consider locally-tagged-but-not-pushed
items as released. Alternatively, check for the presence of
in-progress.json before archiving — a stale state file is positive
evidence that the release is not actually complete.

## Interaction between the three bugs

In isolation, Bug 1 causes a confusing traceback. Bug 2 causes wasted
resume work. Bug 3 causes premature archival. Together, they produce the
v0.11.0 scenario: the push fails (Bug 1 buries the resume message in a
traceback), a subsequent run or the release agent's own retry logic
triggers the repair pass (Bug 3 archives the batch file based on local
evidence), and the user or agent sees "nothing to release" instead of a
resumable state.

## Affected files

- `commands/release/__init__.py` (`run_cmd` exception handling)
- `commands/release/execute.py` (`_handle_resumable_push_failure` retry
  fall-through)
- `commands/monorepo/batch_plan.py` (`item_is_released` /
  `plan_all_released` — local-only tag check)
- `commands/monorepo/batch_release.py` (repair pass at command start)

## Effort

M. Each fix is small (5-15 lines); the work is in the red-green tests
for each failure mode and the interaction test (push fails → retry →
repair pass → resume still works).
