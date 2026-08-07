# Infra-killed CI runs are misclassified as deterministic, stranding resumed releases

## Context

The main-as-candidate engine reuses an existing CI run's verdict for the
candidate SHA. When a release is resumed on an UNCHANGED candidate, the push is
a no-op, so no new CI run is triggered — whatever run already exists for that
SHA is the verdict.

## Problem (observed during a real resumed release, 2026-08-07)

A provider-wide CI outage killed a candidate's run at the infrastructure layer:
every job died on runner acquisition ("The job was not acquired by Runner of
type hosted") or action resolution ("Failed to resolve action download info.
Error: Service Unavailable"); the one job that got a runner passed. On resume
after the outage, the engine found the stale failed run, classified it
"deterministic failure detected; not retrying", and aborted with exit 1. Because
the no-op push can never produce a fresh run, the release was permanently
unrunnable through the tool. The operator escape was a manual
`gh run rerun <id> --failed`, after which the resumed release proceeded and
published cleanly.

## Root cause

The retry classifier treats any failed conclusion on the candidate's run as
deterministic. It does not distinguish job-level failures (real, deterministic,
retrying is wasteful) from infrastructure-level failures (runner never acquired,
action download 5xx, run cancelled while queued), which are precisely the
retryable class.

## Solutions

- (a) Classify infrastructure failures from the failure annotations/step
  context (runner-acquisition, action-resolution 5xx, cancelled-while-queued)
  and have the engine issue `gh run rerun <id> --failed` itself, then wait on
  the rerun — bounded to one rerun attempt, mirroring the watch layer's
  existing single-bounded-retry policy. Pros: self-healing, no new surface.
  Cons: classification signatures need maintaining (the watch layer already
  maintains an extensible signature set — reuse it).
- (b) On resume with a failed run, re-derive the verdict per JOB: if every
  failed job failed pre-execution (never acquired a runner / never resolved
  actions), treat the run as infra-void and rerun. Pros: structural, no string
  matching. Cons: needs the per-job API walk.
- (c) Do nothing; document the manual `gh run rerun` remediation in the abort
  message. Weakest, but the abort message SHOULD name the remedy regardless of
  (a)/(b).

Recommend (a) reusing the watch layer's signature machinery, plus the (c)
message improvement unconditionally.

## Also observed (minor, same release)

The post-release assembly-rebuild hook logged "Dispatched assembly rebuild for
<project> v0.0.0" — the hook env's version for a monorepo workspace-root
invocation is the 0.0.0 placeholder rather than the released member's version.
Harmless but misleading in logs; the hook env should carry the releasable's
real version (or the dispatch line should omit the version when running at a
workspace root).

## Affected

- The release engine's CI-verdict/retry classification for candidate SHAs
  (resume path especially).
- The watch layer's failure-signature set (reuse).
- The abort message for the not-retrying path.
- Post-release hook env version at workspace roots (minor item).

## Effort

Medium — classification + bounded rerun + tests with staged run shapes; the
message fix and hook-env item are small.
