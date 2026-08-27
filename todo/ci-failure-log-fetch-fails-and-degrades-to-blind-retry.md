# Failure-log fetch exits 1, so retry classification runs blind

## Context

When a watched CI run fails, `_fetch_failure_log` in `rlsbl/commands/watch.py`
collects the failing jobs' logs and hands the text to the retry classifier,
which decides whether the failure is deterministic (never retried) or
infrastructure-like (rerun once in place).

The fetch reads each failing job's log through the REST endpoint:

```
gh api --method GET repos/{owner}/{repo}/actions/jobs/<job-id>/logs
```

## Problem

Observed on a live failing run: that invocation exits 1 and returns nothing.
The exception propagates, the watch emits its note about being unable to read
the logs, and the classifier is handed an empty string — so the retry decision
is made with no evidence at all. What is described as classified retry
degrades in practice to a blind retry.

From the same shell, at the same moment, with the same `gh` authentication and
the same repository, this worked and printed the failing job's log:

```
gh run view <run-id> --log-failed
```

So the credentials, the network and the run are all fine; the specific API
invocation is what fails. The likely cause is that the logs endpoint answers
with a redirect to a short-lived blob URL and `gh api` does not follow it the
way the code assumes, but the diagnosis is worth confirming against the actual
response rather than assumed.

The code's docstring explains the choice of endpoint: `gh run view` walks
repo-level Actions collections that 404 on some repositories, while the
id-keyed endpoints do not. That reasoning is sound for the **jobs** lookup, and
the jobs lookup does work — it is only the per-job **log** read that fails. The
current shape trades a failure mode that hits some repositories for one that,
on the evidence, hits the common case.

## Solutions

### Read the failing logs with the invocation that works (recommended)

Fetch the failure text with `gh run view <run-id> --log-failed`, keeping the
existing id-keyed jobs lookup for the job names and conclusions.

- Pro: the classifier gets real input in the case that currently produces none,
  which is the whole point of classifying instead of retrying blindly.
- Pro: the per-job fetch loop and its job cap disappear for the log read — one
  call instead of one per failing job.
- Con: reintroduces the repo-level Actions collection the docstring warns can
  404. Mitigate by keeping the run-id-keyed call rather than a workflow-name
  lookup, and by treating a failure here as the same loud note it is today.
- Con: `--log-failed` returns text for the whole run, so the per-job section
  headers the current code builds have to be derived from that text (or the
  headers built from the jobs lookup and the text attached wholesale).

### Keep the API endpoint and make it follow the redirect

Investigate the actual response and, if it is a redirect, fetch the blob URL
explicitly (`gh api --include` to read the `Location` header, then a plain
fetch of that URL).

- Pro: keeps the id-keyed shape the docstring argues for, and keeps per-job
  granularity.
- Con: two calls per failing job, a short-lived signed URL to handle, and a
  hand-rolled redirect path that has to keep working as the API evolves.
- Con: still unverified — the redirect theory needs confirming before anything
  is built on it.

### Make an empty classification input a hard stop rather than a blind retry

Independently of how the text is fetched: when the classifier receives nothing,
do not retry on a guess. Report that classification had no input and let the
operator decide.

- Pro: removes the silent degradation — a decision made with no evidence stops
  looking like a decision made with evidence.
- Pro: composes with either fix above, and is the safety net if the fetch fails
  for some new reason later.
- Con: an operator interruption on runs that today would quietly rerun and
  pass.

## Affected files

- `rlsbl/commands/watch.py` — `_fetch_failure_log`, `_failure_region`, and the
  classify/retry path that consumes the returned text
- `rlsbl/ci_checks.py` — `fetch_run_jobs`, if the jobs lookup is reshaped
- tests covering failure classification, including a case where the log fetch
  returns nothing

## Effort

Small for the first option plus the empty-input stop. Larger if the redirect is
confirmed and the id-keyed path is kept, because that adds a second call per
job and a signed-URL fetch to maintain.
