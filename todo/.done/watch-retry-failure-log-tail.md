# Watch retry: print the retry run's own failure log tail

Residual slice of the (now `.done/`) publish-watch-retries-deterministic-failures
todo. The classification layer shipped in v0.105.0 (`_DETERMINISTIC_SIGNATURES` /
`_TRANSIENT_SIGNATURES` / `_classify_failure` in `rlsbl/commands/watch.py`):
deterministic failures no longer retry, and the ORIGINAL run's failing-step log tail
is always fetched and printed (`watch.py:321-325`).

## Remaining gap

When a transient/unknown failure IS retried and the retry itself also fails,
`_retry_workflow`'s `except subprocess.CalledProcessError` branch (`watch.py:269-274`)
prints only the retry run's URL — not its log tail. The retry may fail differently
than the original (or identically, which is itself diagnostic); either way the
operator gets a URL instead of the evidence. The original todo's explicit minimum ask
("after a retry fails on the same step, print the failing step's log tail directly
instead of just the run URL") is therefore half-met.

## Fix

In `_retry_workflow`'s failure branch, call `_fetch_failure_log(retry_run_id)` and
print the tail alongside the URL, guarded by try/except so a broken log fetch never
breaks the return path (mirror the loud-note fallback pattern already used in
`_watch_single_run`). One red-green test asserting the retry-failure tail is printed.

## Interaction note

The retry MECHANISM is separately being switched from fresh `gh workflow run`
dispatch to in-place `gh run rerun <failed_run_id>` (decided 2026-07-17, fixes
check-run duplication poisoning the publish gate AND the retry running on branch HEAD
instead of the failed commit). Land this log-tail fix together with or after that
switch — with in-place rerun the run id for the tail fetch is the same id already in
scope.

## Affected files

- `rlsbl/commands/watch.py` (`_retry_workflow`, ~:269-274)
- `tests/test_watch.py` (one new test)

## Effort

Small (~30 min including the test).
