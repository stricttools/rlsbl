# Skip the test suite on deletion-only pushes

## Context

The V5 pre-push hook exports `RLSBL_PUSH_STDIN` (git's per-ref
`local_ref local_sha remote_ref remote_sha` lines) and runs
`rlsbl check --tag prepush`. For a branch deletion
(`git push origin --delete <branch>`), git reports the local ref as
`(delete)` with the zero SHA. `prepush-changelog-coverage` already handles
this correctly: `_get_pushed_commits` skips zero-SHA local refs, so the
commit set is empty and coverage passes trivially. `prepush-manual-warning`
also does not fire (the local ref is not a release branch ref).

The `test-suite` check, however, has no push-context awareness at all: it
runs the full suite (`go test ./... -race -short -count=1` for Go targets,
with `check_timeout` as the ceiling) on every push, including pushes that
carry zero commits.

## Problem

A deletion-only push runs the entire race-mode test suite before deleting a
remote ref. The suite result cannot affect the deletion's correctness --
there are no commits being published -- so the run adds no information.
Costs observed in practice on a consumer project:

- Multi-minute wall-clock for a metadata-only operation (race-instrumented
  rebuild from a cold build cache is silent for a long stretch, so it also
  reads as a hang and invites the user to Ctrl-C mid-hook).
- The user's recourse becomes `--no-verify` or deleting the branch through
  the forge's web UI -- i.e. the guardrail teaches people to route around
  hooks entirely, which is the opposite of its purpose.

## Proposed solutions

### A. Push-context awareness in the check runner (recommended)

When `RLSBL_PUSH_STDIN` is present and ALL its ref lines are deletions
(zero local SHA / `(delete)` local ref), skip the `test-suite` check (log a
one-line "deletion-only push: test suite skipped"). Mixed pushes (any
non-deletion ref) keep the full behavior.

- Pros: precise; keeps the hard-checks philosophy intact for every push
  that publishes commits; no new flags, no user-facing escape hatch.
- Cons: the check runner needs access to the push context (today only the
  prepush-specific checks parse `RLSBL_PUSH_STDIN`).

### B. Give test-suite the same commit-set predicate as coverage

Generalize: any prepush check may declare it is vacuous when the pushed
commit set is empty, and the runner computes the set once. Deletion pushes
then skip everything vacuous by construction.

- Pros: one mechanism instead of a special case; future prepush checks get
  correct deletion behavior for free.
- Cons: larger refactor of the check-runner contract.

### C. Do nothing, document `--no-verify` for deletions

- Pros: zero code.
- Cons: normalizes hook bypass; contradicts the no-escape-hatches
  philosophy; the multi-minute silent run keeps looking like a hang.

## Related consideration (separate decision)

Deletion pushes currently sail past `prepush-manual-warning` entirely
because the local ref is `(delete)`. That means deleting a RELEASE branch
(`git push origin --delete main`) is not warned about at all, which is far
more destructive than the pushes the warning does catch. Worth deciding
whether deletion of a ref in `release_branches` should be a hard error in
the same change.

## Affected files

- `rlsbl/prepush_utils.py` -- push-context parsing (`_get_pushed_commits`
  already models zero-SHA refs; expose a "deletion-only push" predicate)
- `rlsbl/data/checks.toml` -- `test-suite` tag entry, if the skip is
  declared as check metadata
- `rlsbl/testing.py` -- test-suite runner, if the skip lands there
- The `check --tag prepush` dispatch path, to thread the push context

## Effort

Small for option A: one predicate plus one skip branch plus tests (a
deletion-only stdin fixture asserting the suite is skipped, a mixed-push
fixture asserting it is not). Medium for option B.
