# Publish gate: dedup check-runs to latest per name (flaky run permanently blocks publish)

## Context

The scaffold's `publish.yml` gate job collects all check-runs on the release commit matching `^(test)( \(.*\))?$` and hard-fails if any is not successful.

## Problem

When a CI run fails on a flaky test and is retried, the retry semantics matter:

- `gh run rerun` (in place) updates the existing check-run → gate can pass afterward.
- The release flow's automatic retry starts a NEW workflow run, which creates a NEW check-run. The original failed check-run stays permanently attached to the commit. The gate then sees both `test: failure` and `test: success` and refuses forever.

Net effect: any single transient CI failure on a release commit permanently blocks all registry publishes for that release, even after CI has genuinely passed on the identical commit. Observed twice in production on consecutive releases of a consumer project (two different flaky e2e tests; in both cases the retry was green and the gate still refused).

## Solutions

1. **Dedup to latest per check-run name** (recommended): the gate groups matching check-runs by name and considers only the most recent (by started_at or by run attempt) per name. A retried-and-green commit publishes; a genuinely red commit still blocks. This preserves the gate's guarantee exactly while fixing its false-negative.
2. Make the release flow's auto-retry use in-place rerun (`gh run rerun <id>`) instead of dispatching a fresh run, so the failed check-run flips rather than accumulating. Narrower fix; does not help manual retries that create new runs.
3. Both: dedup in the gate (defense) and in-place auto-retry (hygiene).

No bypass flags, no gate removal — the gate's rule stays "publish only on green CI"; the fix is making "green" mean the commit's current CI verdict rather than its history.

## Affected files

Scaffold template for `publish.yml` (gate job), and wherever the release flow implements its CI auto-retry.

## Effort

S–M. The gate change is a small script edit in the workflow template plus a scaffold re-merge story for existing repos.
