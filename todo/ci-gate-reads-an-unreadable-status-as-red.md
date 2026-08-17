# A CI gate that cannot read logs cannot classify, so a transient red is never retried

## What happened

A batch release pushed its candidate, found the CI run, and reported:

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

The verdict was correct -- the run did conclude `failure`. Two of roughly fifty
parallel jobs failed, both on the same thing:

```
##[error]Response status code does not indicate success: 429 (Too Many Requests).
##[error]Failed to download archive 'https://codeload.github.com/astral-sh/setup-uv/tar.gz/<sha>' after 3 attempts.
##[error]Failed to fetch version data: 429 Too Many Requests
```

GitHub rate-limited the action downloads. That is the textbook non-deterministic
failure the classified auto-retry exists for -- and neither half of the recovery
worked, because both shell out to `gh` subcommands that 404 on this repository.

## Why both halves failed

On this repository the **repo-level** Actions endpoints 404 while the
per-workflow, per-run and per-attempt endpoints answer normally, with the same
token:

| Request | Result |
|---|---|
| `gh api repos/OWNER/REPO/actions/runs` | `404 Not Found` |
| `gh run list` (uses the above) | `failed to get runs: HTTP 404` |
| `gh api repos/OWNER/REPO/actions/runs/<id>/jobs` | `404 Not Found` |
| `gh run view <id>` / `--log-failed` (uses the above) | `failed to get jobs: HTTP 404` |
| `gh run rerun <id>` | exit 1 |
| `gh api repos/OWNER/REPO/actions/runs/<id>` | 200, full run object |
| `gh api repos/OWNER/REPO/actions/workflows/<wfid>/runs` | 200, runs listed |
| `gh api repos/OWNER/REPO/actions/runs/<id>/attempts/1/jobs` | 200, every job with its conclusion |
| `gh api repos/OWNER/REPO/actions/jobs/<jobid>/logs` | 200, full log |

The repository is private, Actions is enabled
(`actions/permissions` -> `{"enabled": true}`), it is neither archived nor
disabled, and the token carries `repo` and `workflow`. Whatever produces the
404 on the collection endpoints, **every fact the gate needed was reachable**:
the run's conclusion, each job's conclusion, and each failing job's log --
through endpoints that take the run id rlsbl already had and printed.

## The defect

The classified auto-retry -- "a deterministic failure is never retried,
anything else is rerun once in place" -- silently degrades to *no retry at all*
when the log fetch fails. The message says so plainly ("retrying without
classification") and then the retry itself fails, so a 429 on an action
download aborted a release that a single rerun would have carried.

Two things follow:

1. **The classifier depends on `gh run view --log-failed`**, a single command
   whose failure removes the whole classification. There is no fallback to the
   per-job logs endpoint, which works here.
2. **The retry depends on `gh run rerun`**, likewise. `POST
   repos/OWNER/REPO/actions/runs/<id>/rerun-failed-jobs` is the API underneath
   and is not affected by whatever breaks the collection endpoints.

Neither failure is reported as an outcome of its own: the release ends on
"CI did not pass ... Fix forward on the release branch", which points the
operator at code that CI never showed to be broken. An operator following that
instruction has nothing to fix.

## The same 404 then blocked the release outright

After the transient failure was rerun by hand and CI went green, the release
was re-run. The gate reported `[CI Router] passed` and the very next line was:

```
rlsbl: batch candidate <sha>: [CI Router] passed
Error: gh: Not Found (HTTP 404)
```

No `--- Completing <member> ---` line follows, so the abort is inside the gate
after the watch returned, before the per-member completion loop -- the point
where the gate verifies that each pending member's CI job actually ran on the
candidate. That check enumerates the run's jobs, and on this repository:

| Request | Result |
|---|---|
| `gh api repos/OWNER/REPO/actions/runs/<id>/jobs` | `404 Not Found` |
| `gh api repos/OWNER/REPO/actions/runs/<id>/attempts/<n>/jobs` | 200, all 151 jobs |

So the release cannot complete at all here, on a candidate whose CI is green,
because one endpoint 404s while the attempt-scoped form of the same query
answers. The error reaches the operator as a bare `Error: gh: Not Found
(HTTP 404)` with no indication of which check it belonged to or what it was
looking for. The state file still reads `BRANCH_PUSHED`; nothing is tagged.

This makes option 1 below the decisive one rather than a robustness nicety: the
attempt-scoped endpoint is not a fallback, it is the one that works.

## Possible directions

Listed as options, not a decision:

1. **Reach the run through the id rlsbl already has.** Prefer
   `gh api .../actions/runs/<id>`, `.../attempts/<n>/jobs` and
   `.../actions/jobs/<jobid>/logs` over `gh run list` / `gh run view`. That
   removes the dependency on the collection endpoints for status, per-job
   conclusions and failure logs alike.
2. **Rerun through the API.** `POST .../actions/runs/<id>/rerun-failed-jobs`
   instead of `gh run rerun`.
3. **Make "could not classify" its own outcome.** It is neither the green, red,
   timeout nor not-configured verdict the release flow documents, and it should
   not silently collapse into "red, no retry". At minimum the final error should
   say the classification was unavailable and name the command that failed, so
   the fix-forward instruction is not given for an unclassified failure.
4. **Say which jobs failed.** The gate names the workflow (`CI Router`) but not
   the failing jobs; with fifty jobs in a monorepo router that is a long way
   from the two that actually failed.

## Consequences observed

Nothing was burnt -- the main-as-candidate ordering held, the candidate commit
sits on the release branch untagged, and re-running the release resumes at the
same version. The cost was a release cycle spent on a transient failure the
built-in retry was designed to absorb, plus the manual work of querying GitHub
by hand to discover that the failure was a 429 and not the code.

## Affected files

- the CI gate / watch path (`rlsbl watch`, the release-flow CI gate, and the
  batch orchestrator's shared implementation of it)
- wherever `gh run list`, `gh run view --log-failed` and `gh run rerun` are
  invoked and their non-zero exits interpreted

## Effort

Small for options 2 and 4. Small-to-medium for option 1 (one accessor per fact,
all three endpoints already proven). Medium for option 3 if the verdict enum is
shared across several call sites.
