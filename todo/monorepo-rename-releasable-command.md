# monorepo rename-releasable command

## Context

Renaming a `[[releasables]]` entry in a monorepo workspace is currently a manual multi-step operation. An investigation (2026-07) mapped everything the releasable name touches and produced a verified manual recipe; a consuming monorepo has already performed an equivalent rename by hand using the alias-tag technique (the same technique `create_migration_tag` in `releasable_migration.py` already implements for migrations).

No existing command covers this: `monorepo migrate-releasable` migrates per-package state INTO the releasable model (assumes the final name already exists in workspace.toml), and `monorepo extract-releasable` splits a releasable into a separate repo. Neither renames.

## Problem

The manual rename has two silent failure modes:

- **Missing boundary alias tag**: the next release finds no `<new>@v<version>` tag but a finalized `<version>.jsonl` changelog exists, so the destroyed-tag guard (`commands/release/validate.py:640-707`) hard-errors. (The guard's own error message describes the tag-format-change remedy — the guard exists precisely because this manual step gets forgotten.)
- **Missing publish-gate re-scaffold**: the generated `publish.yml` gate still keys on the old tag prefix (`startsWith(github.ref_name, '<old>@v')`), so releases under the new prefix appear to succeed while **CI silently never publishes**.

Per the guardrail philosophy: when the same bug class keeps needing manual checklists, make the class impossible with a structural fix — a native, preflight-guarded command.

## Proposed command

`rlsbl monorepo rename-releasable <old> <new>` (global `--dry-run` / `--yes` come free from the app-level flags, `__init__.py:159-160`; CLI wiring mirrors `cmd_mono_migrate_releasable` at `__init__.py:~1494`).

| # | Step | Reuses |
|---|------|--------|
| 0 | Preflight: clean tree; explicit mode (`workspace.py:95`); `<old>` exists / `<new>` doesn't (`workspace.py:170`); `releasables/<new>/` dir absent (`workspace_types.py:31`); `<new>` name charset valid and not colliding with any project/releasable name; gh auth (for the tag push) | existing helpers |
| 1 | Resolve current version (`workspace.py:37`) and the commit of `<old>`'s current tag; compute the new-format tag | `tag_glob.py:13`, `_make_tag` (`validate.py:558`) |
| 2 | Rewrite workspace.toml: `[[releasables]] name` and every member's `releasable` field (`members_of`, `workspace.py:261`) | `save_workspace` (`workspace.py:324`) |
| 3 | Create + push the boundary alias tag `<new>@v<version>` at that commit (no GitHub Release for it) | adapt `create_migration_tag` (`releasable_migration.py:626-758`) |
| 4 | `git mv .rlsbl-monorepo/releasables/<old>` to `releasables/<new>` (version, changes/, config.json, hooks/, releases/ move together) | — |
| 5 | Delete the moved `changes/.validated` cache (`changelog/validate.py:146`) to force fresh validation under the new tag glob | — |
| 6 | Re-run monorepo sync to regenerate the publish gate prefixes and `snapshot.json` | `sync.py:233` `_get_monorepo_tag_prefix`, `snapshot.py:14` |
| 7 | Single commit of workspace.toml + moved dir + deleted cache + regenerated workflows/snapshot | — |

## Edge cases the command must handle

- **`tag_format` without `{name}`** (e.g. legacy path-based formats): renaming the releasable does NOT change the tag prefix. The command must branch: format contains `{name}` (tags change, alias tag needed, gate regenerates) vs not (name-only rename, no alias, no gate change). This is a real branch, not a corner.
- **Multiple member projects**: rewrite every member's `releasable` field; the alias tag is per-releasable, not per-member.
- **`releasable = false` / dev_node members**: untouched by construction (`members_of` filters to string matches).
- **`snapshot.json`**: `releasables` section and per-package `releasable` fields are name-keyed — must regenerate or `snapshot --check` fails.
- **`publish-cache.json`**: keyed by project name, not releasable name — no stale keys; router regeneration triggers correctly via hash comparison.
- **In-flight `unreleased.jsonl` entries**: move with the directory and stay valid (commits unchanged; alias tag preserves the range boundary).
- **workspace.toml comment round-tripping**: `save_workspace` (`workspace.py:344-383`) rebuilds the `[[projects]]`/`[[releasables]]` arrays from scratch, losing inline comments inside those tables. Either document this or improve `save_workspace` to edit in place.
- **Historical GitHub Releases**: stay under the old tag names, valid but no longer rlsbl-managed (edit/deprecate/yank compute tags from the current name). The command should print a note.
- **Alias-tag push**: must be tool-mediated (this command), consistent with how release/scrub pushes are handled; `--dry-run` must clearly list the push as a side effect.

## Solutions considered

- **Native command (proposed)**: bundles the seven steps behind preflight guards; both silent failure modes become impossible. Moderate effort; most primitives exist.
- **Documented manual recipe only**: keeps the failure modes; relies on operator discipline. Rejected as the long-term answer by the guardrail philosophy.
- **Extend migrate-releasable with a rename mode**: conflates two unrelated operations behind one command; rejected for explicitness.

## Affected files

- New `rlsbl/releasable_rename.py` (or extend `releasable_migration.py`) with `cmd_rename_releasable`
- CLI wiring in `rlsbl/__init__.py` (near the other monorepo subcommands, ~1494)
- Reuse: `workspace.py` (`save_workspace`, `read_releasable_version`, `members_of`), `tag_glob.py`, `releasable_migration.py` (`create_migration_tag`), `sync.py`, `changelog/validate.py`
- Tests: rename-specific integration test covering the alias tag + gate regeneration (red-green per project convention), alongside the existing `tests/test_*releasable*` suite

## Effort

Moderate — roughly one focused session. New code is mainly preflight validation, the tag-format branch, the alias push, orchestration, `--dry-run` reporting, and tests; the heavy primitives (tag aliasing, workspace serialization, sync) already exist.
