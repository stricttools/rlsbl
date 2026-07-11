# test-suite check: hard ~120 s timeout is not configurable and intermittently fails legitimate suites

## Context

The `test-suite` check (run by `rlsbl check --all` and as a release-preflight gate) executes the project's test command with a fixed internal timeout of roughly 120 seconds.

## Problem

A Go project whose race-enabled test suite legitimately takes ~100 s wall time passes the check on an idle machine but intermittently fails it under concurrent load ("command timed out after 120s") — the margin between legitimate runtime and the hard timeout is too thin, and there is no way to widen it. Because rlsbl checks are hard gates with no bypass (by design, and correctly so), an intermittent timeout aborts otherwise-valid releases nondeterministically. The failure mode is especially confusing because re-running the check standalone usually passes, making it look flaky rather than structural.

## Possible solutions

1. **Config key for the timeout** (e.g. `checks.test_suite.timeout_seconds` in `.rlsbl/config.json`, default 120): explicit, per-project, no bypass semantics changed — the check still hard-fails on real hangs. Pros: minimal, philosophy-compatible (an explicit declared budget is not an escape hatch). Cons: none obvious; a ceiling (e.g. max 600) avoids absurd values.
2. **Scale timeout to a measured baseline**: store the last successful duration and allow, say, 2x. Pros: adaptive. Cons: hidden state, non-deterministic gate behavior — worse than explicit config.
3. **Document a convention instead**: projects must keep the gated suite under 120 s (e.g. via `-short` subsets), full suites run outside the gate. Pros: zero code. Cons: pushes every heavy project into maintaining two test tiers, and weakens what the release gate actually verifies.

Option 1 recommended; option 3 can remain the guidance for projects that prefer fast gates.

## Affected files

- The test-suite check implementation (timeout constant)
- Config schema + docs for the new key

## Effort estimate

Small: thread one configurable value with a default and a sane ceiling through the check, plus a test that a configured value is honored and that absent config keeps 120 s.
