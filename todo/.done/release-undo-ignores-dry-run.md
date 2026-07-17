# `release undo` executes destructively under `--dry-run`

## Context

`--dry-run` is a global flag ("available on all commands", defined at `rlsbl/__init__.py:159`) and users reasonably rely on it to preview destructive operations. `release undo` is among the most destructive commands in the tool: it deletes tags (local and remote), deletes GitHub Releases, creates revert commits, and pushes.

## Problem

`cmd_release_undo` (`rlsbl/__init__.py:763-773`) silently discards the dry-run flag: it lands in `**_kwargs` and the `flags` dict forwarded to `commands/undo.run_cmd` contains only `yes` and `version`. `commands/undo.py` contains no dry-run handling at all (`grep -n dry` returns zero matches). Every destructive operation is unguarded:

- `gh release delete <tag> --yes` — `undo.py:199`
- remote tag delete (`git push origin :<tag>`) — `undo.py:209`
- local tag delete — `undo.py:217`
- companion-tag deletes — `undo.py:244,248`
- `git revert` commits — `undo.py:297,303,310`
- changelog/release-file restore commits — `undo.py:339-340,359-360`
- push — `undo.py:383`
- same for the non-latest path `_run_non_latest_undo` — `undo.py:511-597`

Combined with `--yes` (which both skips the confirmation at `undo.py:179` and enables auto-push at `undo.py:368`), `rlsbl release undo --dry-run --yes` performs a complete, unprompted, destructive undo while the user believes they are previewing.

Observed in a consumer project: a `--dry-run --yes` invocation deleted the latest tag (local + remote), deleted its GitHub Release, and created and pushed a revert commit. A subsequent real undo then resolved the "latest release" to the *previous* tag and deleted that published version's tag and GitHub Release as well.

Every sibling release subcommand threads dry-run correctly (`release run` consults it at `commands/release/__init__.py:550,726,748,767,828,877`; `edit`/`deprecate`/`yank`/`scrub` forward it at `__init__.py:745,784,811,863`). `undo` is the only one that drops it.

## Solutions

1. **Thread dry-run properly (correct fix).** Forward `dry_run` through `run_cmd`, guard every destructive operation, and print the full plan (tag deletions, release deletion, commits to revert, push) without executing. Pros: matches `release run` behavior; dry-run becomes trustworthy tool-wide. Cons: touches many call sites in both undo paths; needs careful test coverage.
2. **Hard-error stopgap.** At `cmd_release_undo` entry, raise a hard error if dry-run is set: "release undo does not support --dry-run yet". Pros: one-line change that immediately eliminates the silent-destruction trap, consistent with hard-errors-over-warnings. Cons: dry-run remains unavailable for undo.

The most correct outcome is (1); (2) is acceptable as an immediate patch preceding it. Doing (2) then (1) in separate releases is reasonable.

## Red-green requirement

Existing undo tests fully mock `run`/`run_gh` (`tests/test_undo.py`, `tests/test_undo_releasable.py`; zero occurrences of "dry" in either), so this class of bug is invisible to them. The regression test must use a real temporary git repository (and a stubbed `gh` boundary): perform a release-shaped history, run undo with dry-run, and assert that tags, branches, commit list, and working tree are byte-identical afterwards.

## Affected files

- `rlsbl/__init__.py:763-773`
- `rlsbl/commands/undo.py` (both `run_cmd` and `_run_non_latest_undo`)
- `tests/test_undo.py`, `tests/test_undo_releasable.py`

## Effort

Medium — roughly half a day including the real-git test harness. The stopgap variant is minutes.
