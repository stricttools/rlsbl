# The auto-retry trigger still fails on repos with broken collection endpoints

## Context

The CI gate's job enumeration was moved to the attempt-scoped jobs endpoint
(`.../runs/<id>/attempts/<n>/jobs`) after a repo was found whose *collection*
endpoints (`runs/<id>/jobs`, `commits/<sha>/check-runs`, `actions/runs`,
`releases`) return 404 while item endpoints answer normally. Enumeration and
failure classification now work on such repos.

## Problem

The retry *trigger* still shells out to `gh run rerun`, which fails on the same
repos. On an affected repo, a transient CI failure is classified correctly but
the automatic rerun cannot be dispatched — the release aborts where it should
have self-healed.

## Solution

Dispatch the rerun through the item-scoped REST call instead:
`POST repos/{o}/{r}/actions/runs/{id}/rerun-failed-jobs` (and `/rerun` for the
whole-run case). Route through the effects seam like every other gh invocation.
One path, no fallback to `gh run rerun`.

## Affected

- `rlsbl/commands/watch.py` (the classified-retry dispatch)
- any other `gh run rerun` / `gh run watch`-adjacent trigger sites (sweep)

## Effort

Small: one dispatch-path swap plus tests mirroring the attempt-scoped
enumeration suite's conventions.
