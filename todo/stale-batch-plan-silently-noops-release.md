# Stale `unreleased.plan.json` silently no-ops the next monorepo release

Two defects hit during a real `rlsbl monorepo release run` on a workspace with a
single releasable. The first is a correctness/data-loss bug; the second is a
dry-run-only crash.

## 1. Stale plan sidecar consumes a fresh release file and exits 0 (serious)

### Symptom

A previous, fully-completed release left
`.rlsbl-monorepo/releases/unreleased.plan.json` behind (it was never archived
with its batch file). On the *next* release attempt, the run printed:

```
Finalized batch release file: batch-<id>.toml
Batch release already complete (all items released per plan); archived the stale batch file and plan.
```

and exited **0**. Nothing was released: the version file was unchanged, no tag
was created, no push happened. Worse, the freshly-authored `unreleased.toml`
(new bump, new description, new context for the *next* version) was renamed to
`batch-<id>.toml` and chmod'd read-only — i.e. the operator's release notes were
consumed and archived by a run that did nothing.

The operator only discovers this by manually checking `git tag` / the version
file, because the exit status is success.

### Root cause

In `rlsbl/commands/monorepo/batch_release.py` (repair pass / resolved-plan
handling), when a plan sidecar exists the code calls
`validate_plan_against_config(plan, batch_config)` and then
`plan_all_released(...)`.

`validate_plan_against_config` compares the plan's items against the batch
config by **name and bump type only**. It does not compare `base_version` or
`target_version`. So a leftover plan describing `0.11.0 -> 0.12.0` validates
cleanly against a config that intends `0.12.0 -> 0.13.0`, because both say
`name = "orxtra-like-releasable"` and `bump = "minor"`. `plan_all_released` then
sees the plan's tag already exists, concludes the batch is complete, and
archives.

The bump type is exactly the field most likely to repeat between consecutive
releases, which makes the false match the common case rather than a corner case.

### Suggested fixes

- `validate_plan_against_config` should compare `base_version` against the
  releasable's *current* version. A plan whose `base_version` does not equal the
  current on-disk version is stale by construction and must be a hard error, not
  a silent match.
- A plan whose every item is already released should be treated as residue from
  a finished release and deleted/archived **without** consuming the current
  `unreleased.toml`. The archival of the plan and the archival of the batch file
  are two different decisions and are currently coupled.
- Whatever path completes a release successfully should delete
  `unreleased.plan.json` as part of finalization, so the sidecar never outlives
  its batch.
- Exiting 0 after doing nothing is the most damaging part. "Already complete"
  should exit non-zero (or at minimum print a loud, unmissable notice naming the
  version it thought was released vs. the version the config asked for).

### Reproduction sketch

1. Complete a monorepo release of a releasable (say `X` at `0.12.0`).
2. Ensure `.rlsbl-monorepo/releases/unreleased.plan.json` still exists afterward
   (this is the leftover state; the completed release did not clean it up).
3. Author a fresh `unreleased.toml` for `0.13.0` with the same `bump = "minor"`.
4. Run `rlsbl monorepo release run --no-allow-dirty --watch --approve-consequential`.
5. Observe: exit 0, no tag, no version bump, and `unreleased.toml` gone.

Recovery is manual: re-author `unreleased.toml` from the archived
`batch-<id>.toml` and re-run.

## 2. `monorepo release run --dry-run` crashes with `Error: fileno`

### Symptom

```
$ rlsbl monorepo release run --no-allow-dirty --no-watch --dry-run
Detected selfdoc.json at workspace root
Would run: selfdoc gen --no-auto-commit
Batch release: 1 releasable(s)
Release order: <name>
DRY RUN — no changes were made. Would do:
  1. mkdir: .../.rlsbl-monorepo
error: dry-run preview ends at step 2: monorepo.release.run aborted — the preview above may be incomplete
Error: fileno
```

Exit code 1. Reproducible with stdout on a pipe and on a real pty, so it is not
a TTY-detection artifact. The live (non-dry-run) path is unaffected.

### Root cause

`Error: fileno` is the message of an `io.UnsupportedOperation` raised by calling
`.fileno()` on an in-memory buffer. In preview mode, `rlsbl/effects.py`
substitutes `_RecordedWriter(io.StringIO)` for real file handles (its docstring
notes `open_write` "hands streaming writers a real file object in live mode",
and accumulates in memory otherwise). Something in the step-2 path hands that
recorded writer to a consumer that requires a real file descriptor — a
`subprocess` stdout/stderr target being the most likely candidate.

### Effect

`--dry-run` is unusable for `monorepo release run`, so the preview cannot be
used to sanity-check a release before executing it. Given defect 1 above, the
dry run is exactly the tool an operator would reach for, and it is the one that
does not work.

### Suggested fix

Guard the subprocess/fd handoff in preview mode: either skip the subprocess
entirely (it is a preview — it should only be recorded, not run) or give it a
real temporary fd and discard the output. A regression test that runs
`monorepo release run --dry-run` end-to-end on a fixture workspace would have
caught this.

## Effort

Defect 1: small-to-moderate. The version comparison in
`validate_plan_against_config` is a few lines; decoupling plan archival from
batch-file archival and deleting the sidecar at finalization is the larger part.
Worth a red-green regression test that stages a stale sidecar and asserts the
release either proceeds correctly or fails loudly, and specifically that
`unreleased.toml` survives.

Defect 2: small, once the offending call site is located.
