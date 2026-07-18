# rename-releasable: resume after pre-commit crash pushes tag over broken state

## Context

An independent audit (2026-07-18, rlsbl 0.108.0) of `rlsbl monorepo rename-releasable <old> <new>` verified the feature end-to-end on scratch fixtures. R1-R9 pass, preflight guards are solid, comment preservation works, and the post-commit crash window heals correctly (covered by `TestCrashHealing`). One genuine defect was found and functionally reproduced.

## Problem

The resume detector (`rlsbl/commands/monorepo/releasable_rename.py:307,310`) keys purely on working-tree/disk state (`new_present and not old_present and dir_moved`). That state is identical whether or not the single rename commit (`:209-214`) ever landed, so the resume path cannot distinguish "committed, tag push pending" from "mutated, never committed." The resume branch also skips the clean-tree preflight and never re-runs sync.

Consequence: a crash in the window after the directory move (`:182-185`) but before the commit (`:209-214`) leaves a state where re-running the command:

- pushes the boundary alias tag to origin,
- leaves workspace.toml and the directory move uncommitted (dirty tree),
- leaves the regenerated-never `publish.yml` gate on the STALE old prefix,
- exits 0 and reports success.

The stale gate prefix is exactly the silent-no-publish failure mode this command exists to prevent, reintroduced through its own resume branch.

## Reproduction (verified against 0.108.0)

1. Build a monorepo fixture with releasable `alpha` at version 0.5.0, tag `alpha@v0.5.0`, local bare origin, generated publish.yml gating on `alpha@v`.
2. Simulate the mid-crash state: apply the workspace.toml rename edit and `os.rename` the releasables dir `alpha` to `zeta`, but do NOT commit.
3. Run `rlsbl monorepo rename-releasable alpha zeta`.
4. Observed: exit 0, "Alias tag pushed: zeta@v0.5.0"; `git status` shows the uncommitted edits; publish.yml still contains `startsWith(... 'alpha@v')`.

## Suggested fix

In the resume branch, before finishing the alias tag, either:

- (a) require a clean working tree AND verify the rename commit actually exists (e.g. the workspace.toml rename is committed, not just on disk); if the tree is dirty with rename-shaped changes, complete the original sequence instead (invalidate publish-cache, re-run sync, commit everything, then push); or
- (b) unconditionally re-run the tail of the normal path from the sync step (idempotent) and commit any residual changes before pushing.

Either way, per the red-green convention: first add a failing test for the pre-commit crash window (mirror of `TestCrashHealing`, but simulating the crash before the commit instead of after it), then fix.

## Secondary observation (same command, decide separately)

`--yes` is accepted and threaded through (`releasable_rename.py:281`) but never referenced in the body: no confirmation prompt exists anywhere, including before the destructive tag push. Either add a confirmation gate that `--yes` bypasses (consistent with how destructive operations are treated elsewhere), or stop accepting the flag for this command.

## Affected files

- `rlsbl/commands/monorepo/releasable_rename.py` (resume detection `:307-340`, tail sequence `:182-238`)
- `tests/test_releasable_rename.py` (new pre-commit crash test)

## Effort

Small. The fix is a stricter resume predicate plus reusing the existing tail sequence; the test mirrors an existing one.
