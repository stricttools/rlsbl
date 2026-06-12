# Monorepo release path not auto-exempted in changelog coverage

## Problem

`_is_changelog_path()` in `rlsbl/changelog/validate.py` auto-exempts commits that only touch changelog infrastructure files (`.rlsbl/changes/`, `.rlsbl/releases/`, `.rlsbl/version`, `CHANGELOG.md`). But it does NOT recognize `.rlsbl-monorepo/releases/` paths.

When a monorepo batch release prep commit touches both workspace-level (`.rlsbl-monorepo/releases/unreleased.toml`) and project-level (e.g., `python/.rlsbl/releases/unreleased.toml`) files, the workspace-level path prevents the commit from being auto-exempted. This forces manual `--no-user-facing` entries for release prep commits that should be fully mechanical.

## Reproduction

1. In a monorepo, edit `.rlsbl-monorepo/releases/unreleased.toml` and a sub-project's `.rlsbl/releases/unreleased.toml` in the same commit
2. Run `rlsbl check --tag changelog` in the sub-project
3. The commit shows as uncovered despite only touching release infrastructure

## Fix

Add `.rlsbl-monorepo/releases/` (and possibly `.rlsbl-monorepo/` broadly) to the exemption patterns in `_is_changelog_path()`.

## Effort

Small. One-line pattern addition + test.
