# The CI gate reported red for a run that was still in progress

## What happened

A batch release pushed its candidate and the gate reported a red verdict while
the run had not concluded:

```
Found 1 CI run(s) for <sha>; waiting for completion...
rlsbl: batch candidate <sha>: [CI Router] FAILED
rlsbl: https://github.com/OWNER/REPO/actions/runs/<id>
rlsbl: batch candidate <sha>: [CI Router] could not fetch failure logs: Command '['gh', 'run', 'view', '<id>', '--log-failed']' returned non-zero exit status 1.; retrying without classification
rlsbl: batch candidate <sha>: [CI Router] CI failed, retrying once...
rlsbl: batch candidate <sha>: [CI Router] retry trigger failed: Command '['gh', 'run', 'rerun', '<id>']' returned non-zero exit status 1.
Error: CI did not pass on the batch release candidate <sha>.
  Failing workflow(s): CI Router
```

At that moment, and for several minutes afterwards, the run was healthy:

```
$ gh run view <id> --json status,conclusion --jq '{status,conclusion}'
{"conclusion":"","status":"in_progress"}
```

Seven of eight jobs had concluded `success`; the eighth (a long
cross-language conformance job) was mid-step. `gh run view <id>` and
`gh run view --job=<jobid>` both answered normally with the same token, so this
is not the repo-level-404 shape recorded in
`ci-gate-reads-an-unreadable-status-as-red.md` -- the endpoints work here, and
the verdict itself was simply wrong.

The two follow-up `gh` failures are consistent with a healthy in-progress run
rather than with a broken API: `--log-failed` has no failed job to read, and
`gh run rerun` refuses a run that has not finished.

## Cause (as far as the source shows)

`_watch_single_run` (`rlsbl/commands/watch.py`) runs:

```python
run_gh(["run", "watch", run_id, "--exit-status"], timeout=timeout)
```

and treats **any** `CalledProcessError` as the red verdict. But
`gh run watch --exit-status` exits non-zero for two unrelated reasons:

1. the run concluded and it was a failure -- the verdict the gate wants;
2. the watcher itself could not continue -- a transient API error, a 5xx, a
   dropped connection, a rate-limited poll -- while the run is still going.

Only the first is a verdict. The second is the watcher losing its stream, and
the run may still be running (as here) or may still go on to pass.

Conflating them turns a network hiccup into a red release gate. Under
main-as-candidate ordering nothing is burnt, so the damage is bounded -- but
the operator is told to fix forward code that is not broken, and the accurate
state ("the watcher lost the stream; the run is still going") is never printed.

## What the fix has to establish

After `gh run watch` exits non-zero, **re-read the run's own state** before
calling it red:

- `gh api repos/OWNER/REPO/actions/runs/<id>` -> `status` and `conclusion`.
- `status != "completed"` means the watcher dropped, not that CI failed. Resume
  watching (or fall back to polling that endpoint) until it does complete.
- `status == "completed"` with `conclusion == "success"` is green, whatever the
  watcher's exit code said.
- `status == "completed"` with a failing conclusion is the only red.

This also removes the two nonsense follow-ups: neither `--log-failed` nor
`rerun` should be attempted against a run that has not concluded.

The four-verdict vocabulary the release gate already documents (green / red /
timeout / not configured) has no member for "the watcher lost its stream",
which is why it collapsed into red. Either resolve it into one of the four by
re-reading the run, or give it a fifth spelling that says what it is -- the
first is better, because the run's own state is authoritative and cheap to ask
for.

## Reproducing

Hard to force directly; the shape is any repo whose CI outlasts a transient
`gh run watch` interruption. A test can inject a `CalledProcessError` from
`run_gh(["run", "watch", ...])` while a stubbed
`repos/.../actions/runs/<id>` answers `{"status": "in_progress"}`, and assert
the gate keeps waiting instead of returning `passed: False`.

## Affected files

- `rlsbl/commands/watch.py` (`_watch_single_run`, and the retry path it enters)
- whatever the batch orchestrator's gate reads from it

## Effort

Small-to-medium: one re-read plus a resume loop, the retry path made
conditional on a concluded run, and two regression tests (watcher drops with
the run in progress; watcher drops with the run already green).
