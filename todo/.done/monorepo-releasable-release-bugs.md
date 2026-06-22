# Monorepo releasable release bugs

Three bugs discovered during orxt's first release (monorepo with 16 sub-projects, one releasable). All three were hotfixed in the rlsbl source (editable install) to unblock the release. The fixes are in the working tree but uncommitted.

## 1. Changelog validation uses single-project scope instead of all-member scope

During `rlsbl monorepo release`, `validate_changelog_state` receives `monorepo_project` as a single project dict (the representative member). The `check_in_range` and `check_coverage` functions then filter commits to only those touching that one project's path. But the releasable-level changelog references commits from ALL member projects. Commits touching other members are filtered out, causing false "not in unreleased range" and "uncovered commit" failures.

- Location: `commands/release/__init__.py` line ~177
- Fix applied: when `releasable_name` is set, skip setting `monorepo_project` entirely (pass `None` to disable path filtering). This is correct for single-releasable monorepos where every commit is in scope. For multi-releasable monorepos, this needs refinement (pass the member project list instead).
- The infrastructure already exists: `_filter_commits_for_scope` dispatches on list vs dict, and `filter_commits_for_releasable` handles project lists. The gap was that the release flow never passed a list.

## 2. Workspace root lockfile not synced after version bump

`_sync_lockfiles` iterates over `target_paths` (sub-project directories) looking for lockfiles. In a uv workspace, the lockfile lives at the workspace root, not in sub-project directories. After bumping versions in all member pyproject.toml files, the root `uv.lock` becomes stale. When it updates (via background uv resolution or another process), the concurrent-change guard flags it as "Unexpected modified files" and aborts the release.

- Location: `commands/release/execute.py` line ~587
- Fix applied: after the per-project lockfile sync, also run `_sync_lockfiles` on the workspace root directory, AND unconditionally add the workspace root's `uv.lock` to `files_to_commit` so the concurrent-change guard expects it.
- The unconditional inclusion was needed because `_sync_lockfiles` only adds a lockfile if its mtime changes during the sync command. If the lockfile was already modified by the version bump (before sync runs), the mtime comparison doesn't detect a change, so it's not added to the expected files list.

## 3. CI publish template missing `--out-dir dist`

The CI publish template (`templates/pypi/publish.yml.tpl`) runs `uv build` without `--out-dir dist`. In a uv workspace, `uv build` from a sub-project directory outputs to the workspace root's `dist/`, not the sub-project's `dist/`. The `pypa/gh-action-pypi-publish` action then looks at `<project>/dist/` and finds nothing.

- Location: `templates/pypi/publish.yml.tpl` line 18
- Fix applied: changed `uv build` to `uv build --out-dir dist`
- Irony: rlsbl's own local `build()` method in `targets/pypi.py` already uses `--out-dir dist`. The CI template was the only place missing it.
- After fixing the template, `rlsbl scaffold --force` did NOT propagate the change to per-project publish.yml files (three-way merge preserved the old content). The per-project files and their bases were updated manually via sed.

## Uncommitted state

All three fixes are in the rlsbl working tree (editable install, so they took effect immediately). They need to be committed, tested, and released.
