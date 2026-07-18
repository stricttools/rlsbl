# Monorepo publish-router dispatch bugs

Two bugs in the monorepo publish workflow router generator, both observed in rlsbl-sandbox monorepo (pkga@v0.1.2 release). The dispatch mechanism itself works correctly -- the issue is in the generated workflow's internal resolution logic.

## Bug 1: Gate SHA mismatch

**Symptom:** Monorepo publish gate times out after 5 minutes waiting for CI check-runs that never appear on the resolved commit.

**Root cause:** The gate resolves the release commit via `GITHUB_SHA` (the tag commit). But monorepo releases create post-tag commits (changelog finalization, snapshot), so the tag commit is NOT the push HEAD. CI runs on push HEAD, not on each individual commit. The gate looks for CI check-runs on the tag commit and finds none -- timeout -- publish blocked.

This affects both release-triggered and tag-ref `workflow_dispatch`.

**Affected code:**

- `publish_gate.py` -- `GATE_POLL_SCRIPT` resolves `GITHUB_SHA`
- `publish_inline.py` -- gate job env passes the wrong SHA

**Fix direction:** The gate must resolve the PUSH commit (the commit CI actually ran on) instead of the tag commit. Options:

1. The release flow records the push SHA in the GitHub Release body or a release asset and the gate reads it.
2. The gate walks from the tag to the branch tip looking for the nearest commit with CI check-runs.
3. The gate uses the branch HEAD at dispatch time instead of the tag SHA.

## Bug 2: Job if-conditions ignore inputs.tag

**Symptom:** Inlined publish jobs are skipped when the workflow is dispatched at `ref=main` because the job-level guard uses `if: startsWith(github.ref_name, 'pkga@v')` which evaluates against `ref_name="main"` and fails.

**Root cause:** The checkout step correctly uses `inputs.tag || github.event.release.tag_name` but the job-level `if` condition does not -- it only checks `github.ref_name`.

**Affected code:**

- `publish_inline.py` -- inlined job conditions

**Fix:** Change job-level conditions to `if: startsWith(inputs.tag || github.ref_name, 'pkga@v')`, matching the pattern the gate already uses.

**Effort:** Small. The correct pattern already exists in the gate; it just needs to be applied to the inlined job conditions too.
