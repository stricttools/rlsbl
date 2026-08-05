# The publish gate's `rlsbl-ci-sha` marker read fails 100% of the time

## Context

`rlsbl` writes the exact commit CI ran on into the GitHub Release body as a
machine-parseable marker:

```
<!-- rlsbl-ci-sha: <40-hex> -->
```

The generated publish gate is documented to prefer that marker over
`$GITHUB_SHA`, because the marker "pins the precise commit and is immune to ref
races". The gate reads it with a retry loop (`GATE_MARKER_ATTEMPTS`, default 5,
`GATE_MARKER_RETRY_SECONDS` apart, default 5s) whose stated purpose is to absorb
GitHub read-replica lag on a just-created release. On exhaustion it falls back to
`$GITHUB_SHA`.

## Problem

The marker read never succeeds. Not intermittently — **never**. The marker path
is dead code in every generated publish workflow, and every gate silently runs on
the fallback.

Two independent causes, both in `rlsbl/publish_gate.py`:

1. **No repo context.** The gate job has no `actions/checkout` step and sets no
   `GH_REPO`. `gh release view "$tag"` cannot determine the base repository and
   exits non-zero. The sibling call in the same script works only because it
   names the repo explicitly (`gh api "repos/$GITHUB_REPOSITORY/..."`).

2. **Insufficient token scope.** `build_gate_job()` sets
   `"permissions": {"checks": "read"}` (`rlsbl/publish_gate.py:300`). Reading a
   release body needs `contents: read`, which is never granted. So even with
   repo context the call would fail.

Both failures are then **swallowed**: the read is written as

```sh
if body="$(gh release view "$tag" --json body --jq .body 2>/dev/null)"; then
```

`2>/dev/null` discards the error, and a hard, permanent, misconfiguration-class
failure becomes indistinguishable from "the marker is not visible yet". The gate
prints a message blaming replica lag, sleeps 25 seconds, and silently degrades to
`$GITHUB_SHA` — on every run, in every repo.

Costs:

- **25 seconds burned per publish job, always**, for a retry loop that can never
  succeed.
- **The stated guarantee is not delivered.** Every gate in the fleet runs on the
  fallback path. Any scenario the marker exists to handle (CI verified on a
  commit other than the tag's own) is unprotected right now.
- **The log actively misleads.** It names a plausible transient cause, so an
  operator debugging a failed publish spends time on replica lag instead of the
  real defect. This happened.
- This is silent runtime degradation of exactly the kind the fleet forbids: the
  same input produces a quietly different code path with no caller signal.

## Evidence

Six gate runs across two separate releases of the same three-releasable
monorepo — 3 members × 2 releases — **all six** exhausted the retry and fell
back. Representative (identical shape in all six):

```
Publish gate: rlsbl-ci-sha marker not yet visible in the '<tag>' release body (attempt 1/5); retrying in 5s...
Publish gate: rlsbl-ci-sha marker not yet visible in the '<tag>' release body (attempt 2/5); retrying in 5s...
Publish gate: rlsbl-ci-sha marker not yet visible in the '<tag>' release body (attempt 3/5); retrying in 5s...
Publish gate: rlsbl-ci-sha marker not yet visible in the '<tag>' release body (attempt 4/5); retrying in 5s...
Publish gate: no rlsbl-ci-sha marker after 5 attempt(s) on the '<tag>' release body; falling back to $GITHUB_SHA.
```

The marker was present in the release body the entire time. Read from a
workstation, immediately after and long after:

```
$ gh release view <tag> --json body --jq .body | grep -n rlsbl-ci-sha
3:<!-- rlsbl-ci-sha: <same-sha-the-gate-fell-back-to> -->
```

It is line 3 of the body — written at release creation, exactly as designed. A
100% miss rate across two releases and six jobs, on a body that demonstrably
contains the marker, is not lag.

Note on blast radius: in these particular releases the fallback happened to
resolve to the *same* SHA as the marker (for a `release` event, `$GITHUB_SHA`
is the tag's commit, and the batch had tagged the gated candidate). So the
fallback did not itself cause the publish failures that were observed — a
separate paths-filter defect did. That is luck, not correctness: the marker
exists precisely for the cases where the two differ, and in those cases the gate
would today verify CI on the wrong commit without saying so.

## Solution options

### Option A — Fix the read, then make failure refuse

Grant `contents: read` in `build_gate_job()`, give the step repo context
(`GH_REPO: ${{ github.repository }}` — cheaper than a checkout), stop discarding
stderr, and distinguish the three outcomes:

- read succeeded, marker present → use it
- read succeeded, marker absent → this is a genuinely old release; fall back and
  say so plainly
- read failed → **hard error**, do not fall back

- Pros: fixes the defect and closes the class; a broken gate can never again
  masquerade as a lagging one; keeps the one legitimate fallback (pre-marker
  releases) explicit and narrow.
- Cons: needs care so pre-marker releases still publish; distinguishing "404 no
  such release" from "403 no permission" from "no base repo" means inspecting
  `gh` exit status and stderr.

### Option B — Fix the read only

Grant `contents: read` and set `GH_REPO`, leave the fallback as is.

- Pros: minimal diff; immediately restores the intended behavior and reclaims
  the 25 seconds.
- Cons: leaves the silent-degradation shape intact, so the next
  permissions/context regression is equally invisible. Guardrail keeps the same
  hole.

### Option C — Refuse unconditionally; drop the fallback

Require the marker. No `$GITHUB_SHA` path at all.

- Pros: strongest and simplest guarantee — the gate verifies CI on the commit
  rlsbl says CI ran on, or it refuses. No escape hatch, matching the fleet's
  no-fallback philosophy.
- Cons: any release created before the marker existed becomes unpublishable via
  dispatch-retry. Acceptable only with a documented migration (or is fine
  outright if no such releases are still retried in practice — worth checking
  before choosing).

### Option D — Longer window / exponential backoff

Raise attempts or back off.

- Pros: none relevant.
- Cons: **does not fix anything.** The read is broken, not slow. This would only
  lengthen the guaranteed dead wait. Listed explicitly so it is not mistaken for
  a fix — the original retry loop is itself an instance of this error.

### Recommendation shape

A, with C as the more correct end state if pre-marker releases are confirmed
irrelevant. D is a trap. Whatever is chosen, the `2>/dev/null` must go: it is
what converted a permanent misconfiguration into an invisible one.

## Affected files

- `rlsbl/publish_gate.py` — marker read loop (~line 148), `build_gate_job()`
  permissions (~line 300), `build_router_gate_job()`
- `rlsbl/commands/release/execute.py` — marker write side (verify the marker is
  written before the publish workflow can start)
- `rlsbl/commands/monorepo/publish_inline.py` — inline monorepo gate generation
- `tests/test_publish_gate_script.py`, `tests/test_ci_sha_marker.py` — existing
  coverage that evidently does not exercise the real permission/repo-context
  path
- Regenerated `publish.yml` in every scaffolded consumer

## Effort estimate

~2-4 hours. The code change is small; the work is in testing. Existing tests pass
today while the feature is 100% broken in production, so the valuable deliverable
is a test that would have caught that: assert the generated gate job grants
`contents: read` and supplies repo context, and assert the script treats a failed
read as a hard error distinct from an absent marker. Add a red-green regression
test reproducing the swallowed-failure path before changing behavior.
