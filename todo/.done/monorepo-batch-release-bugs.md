# Monorepo batch release: redundant validation, lock file interaction, and missing scaffolding

## Problem

`rlsbl monorepo release` fails with "working tree is not clean" even when `git status --porcelain` shows a clean tree. The root cause is a chain of issues in the batch release flow.

## Bug 1: Redundant validation in batch mode

`run_cmd()` in `rlsbl/commands/release/__init__.py` re-runs `validate_clean_tree()`, `validate_gh_cli()`, and `validate_branch_and_remote()` per project even when called from the batch orchestrator with `batch-mode: True`. The batch orchestrator already validates the tree at the batch level (`batch_release.py` line 98). The `batch-mode` flag is passed but only used to skip the CI watch step — never to skip redundant validation.

This means any file created between batch-level and per-project validation (like `.rlsbl-monorepo/lock`, acquired at `batch_release.py` line 172) causes a spurious "working tree is not clean" failure. The lock file is gitignored by the scaffold template, but projects with stale gitignores hit this. Even with a correct gitignore, the redundant validation is wasted work and a fragile coupling.

**Fix:** Skip `validate_clean_tree`, `validate_gh_cli`, and `validate_branch_and_remote` in `_run_cmd_inner` when `batch-mode` is True.

## Bug 2: Per-project release file finalization dirties the tree between sequential releases

The execute phase (`execute.py` line 782) finalizes the per-project `unreleased.toml` (renames to `vX.Y.Z.toml`, creates fresh empty `unreleased.toml`) as part of each release. In batch mode, this creates new files between sequential project releases, which — combined with Bug 1's redundant validation — can cause the second project's `validate_clean_tree` to fail because the first project's finalization dirtied the tree.

**Fix:** Either skip per-project release file finalization in batch mode (the batch orchestrator should handle it), or commit the finalized files before proceeding to the next project.

## UX gap 1: No `rlsbl monorepo release init`

There is no command to scaffold the batch release file at `.rlsbl-monorepo/releases/unreleased.toml`. The user must reverse-engineer the format from previous batch files. `rlsbl release init` scaffolds per-project release files, but `rlsbl monorepo release` reads a different file.

**Fix:** Add `rlsbl monorepo release init` that generates `.rlsbl-monorepo/releases/unreleased.toml` pre-filled with all releasables listed, their `include`/`exclude` from config, and empty `bump`/`description`/`context` fields to fill in.

## UX gap 2: Per-project vs batch release file confusion

`rlsbl release init` (per-project) and `rlsbl monorepo release` (batch) use different files. The relationship is not obvious. A user who runs `rlsbl release init` in each sub-project creates per-project files that are ignored during batch release. The batch orchestrator passes its own `ReleaseConfig` from the batch file, so per-project files are dead weight.

**Fix:** Either (a) `rlsbl release init` in a monorepo project warns that batch mode uses a different file, or (b) `rlsbl monorepo release` falls back to reading per-project files when no batch file exists, or (c) document the distinction clearly in `rlsbl release init` output.

## UX gap 3: Stale gitignore after scaffold template update

The scaffold template (`rlsbl/templates/shared/gitignore.tpl`) has `.rlsbl-monorepo/lock` but projects scaffolded before this was added don't have it. Re-scaffolding (`rlsbl scaffold`) would fix it, but there's no mechanism to detect or warn about stale gitignores.

**Fix:** `rlsbl check` could verify that the project's `.gitignore` contains all entries from the current scaffold template, warning when entries are missing.

## Discovered in

strictcli monorepo with two releasables (strictcli, go-strictcli). Clean tree, both changelogs covered, batch file correct — `rlsbl monorepo release --watch --yes` fails on the first project with "working tree is not clean" because the lock file was created between the batch-level validation and the per-project validation.
