# The CI gate turned an unreadable run status into a red verdict

## What happened

A batch release pushed its candidate, found the CI run for it, and then
declared the gate red:

```
Pushed the batch release candidate 4d59e0ca92bd to origin/main (untagged, 1 member(s))
Waiting for CI on the release candidate 4d59e0ca92bd (1 push-triggered workflow file(s): ci-router.yml)...
Found 1 CI run(s) for 4d59e0ca92bd; waiting for completion...
rlsbl: batch candidate 4d59e0ca92bd: [CI Router] FAILED
rlsbl: https://github.com/smm-h/<repo>/actions/runs/<id>
rlsbl: batch candidate 4d59e0ca92bd: [CI Router] could not fetch failure logs: Command '['gh', 'run', 'view', '<id>', '--log-failed']' returned non-zero exit status 1.; retrying without classification
rlsbl: batch candidate 4d59e0ca92bd: [CI Router] CI failed, retrying once...
rlsbl: batch candidate 4d59e0ca92bd: [CI Router] retry trigger failed: Command '['gh', 'run', 'rerun', '<id>']' returned non-zero exit status 1.
Error: CI did not pass on the batch release candidate <sha>.
  Failing workflow(s): CI Router
```

The run had not failed. Queried directly at the moment rlsbl gave up:

```json
{"conclusion": null, "run_attempt": 1, "status": "in_progress"}
```

`run_attempt: 1` and `conclusion: null` -- it was never rerun and had not
concluded. The release aborted at the gate on a verdict that was not the run's.

## Why the probe failed

On this repository the **repo-level** workflow-runs endpoint 404s while the
per-workflow and per-run endpoints answer normally, with the same token:

| Request | Result |
|---|---|
| `gh api repos/OWNER/REPO/actions/runs` | `404 Not Found` |
| `gh run list` (uses the above) | `failed to get runs: HTTP 404` |
| `gh run view <id>` (uses `.../runs/<id>/jobs`) | `failed to get jobs: HTTP 404` |
| `gh api repos/OWNER/REPO/actions/workflows` | 200, one workflow |
| `gh api repos/OWNER/REPO/actions/workflows/<wfid>/runs` | 200, runs listed |
| `gh api repos/OWNER/REPO/actions/runs/<id>` | 200, full run object |

The repository is private, Actions is enabled
(`actions/permissions` -> `{"enabled": true}`), the repo is neither archived
nor disabled, and the token carries `repo` and `workflow`. Whatever produces
the 404 on the collection endpoint, the state of the run is plainly readable
through two other endpoints rlsbl already has the run id for.

## The actual defect

Whatever the API quirk, the gate's own logic is what turned it into a bad
release outcome: **a status it could not read became `FAILED`.** The release
flow documents four verdicts -- green, red, timeout, not-configured -- and this
was none of them; it was "the probe errored". Red is the one verdict that stops
a release and tells the operator to go fix code, and it is the wrong answer for
a run that is still running.

The two follow-up messages show the same conflation twice more: the
`--log-failed` fetch failed and was reported as "could not fetch failure logs
... retrying without classification" (so the FAILED verdict was already fixed
before anything had been classified), and `gh run rerun` failed too -- a rerun
that would have been wrong anyway, since the run was mid-flight.

## Possible directions

Listed as options, not a decision:

1. **A probe error is not a verdict.** Distinguish "the run concluded failure"
   from "the status could not be read". The latter should retry with backoff and
   then surface its own error naming the failed command -- never `FAILED`, and
   never a rerun.
2. **Read the run rlsbl already found.** The gate has the run id (it prints the
   URL). `gh api repos/OWNER/REPO/actions/runs/<id>` returns `status` and
   `conclusion` directly and worked here throughout. Preferring the per-run
   endpoint over `gh run list` / `gh run view` removes the dependency on the
   collection endpoint entirely.
3. **Never rerun a run that has not concluded.** `status != "completed"` should
   make the retry path unreachable regardless of how the verdict was reached.
4. **Say which command produced the verdict.** The red message names the
   workflow but not the probe, so an operator cannot tell a real red from an
   unreadable one without querying GitHub by hand.

## Consequences observed

Nothing was burnt -- the main-as-candidate ordering held, the candidate commit
sits on the release branch untagged, and re-running the release resumes at the
same version. The cost was a wasted release cycle and a misleading instruction
to "fix forward" against code that was never shown to be broken.

## Affected files

- the CI gate / watch path (`rlsbl watch`, the release-flow CI gate, and the
  batch orchestrator's shared implementation of it)
- wherever `gh run list`, `gh run view --log-failed` and `gh run rerun` are
  invoked and their non-zero exits interpreted

## Effort

Small for options 2 and 3. Small-to-medium for option 1, depending on how many
call sites share the verdict enum.
