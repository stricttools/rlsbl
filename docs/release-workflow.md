---
description: "Complete reference for the rlsbl release flow — prerequisites, pipeline order, hooks, flags, dev node releases, and related commands."
---

# Release workflow

## Overview

`rlsbl release run` orchestrates the full release lifecycle: validates the project state, bumps the version, runs quality checks, commits, tags, pushes, creates a GitHub Release, finalizes the changelog, and optionally watches CI. The entire flow is driven by a release file (`.rlsbl/releases/unreleased.toml`) that declares the bump type, description, and optional context.

Releases are atomic — if any step fails, the release aborts without leaving partial state. Use `rlsbl release undo` to revert a release that succeeded locally but failed in CI.

## Prerequisites

Before running `rlsbl release run`, ensure:

| Requirement | How to verify | What happens if missing |
| --- | --- | --- |
| Clean working tree | `git status --porcelain` is empty | Hard error (use `--allow-dirty` to override) |
| `gh` CLI authenticated | `gh auth status` | Hard error |
| Changelog coverage | `rlsbl check --tag changelog` passes | Hard error during validation step |
| Release file exists | `.rlsbl/releases/unreleased.toml` present | Hard error (run `rlsbl release init`) |
| Description set | `description` field in unreleased.toml is non-empty | Hard error |

## The release file

Scaffold the release file with `rlsbl release init`, which auto-detects targets and generates a template:

```toml
# .rlsbl/releases/unreleased.toml
bump = "patch"
description = "Short summary of what this release contains"
context = """
Optional multiline explanation of why these changes were made.
Appears as a collapsible details block in CHANGELOG.md.
"""

[include]
targets = ["pypi", "npm"]
```

### Bump types

| Bump | When to use | Version example |
| --- | --- | --- |
| `patch` | Bug fixes, small improvements, no API changes | 0.5.2 -> 0.5.3 |
| `minor` | New features, backward-compatible additions | 0.5.2 -> 0.6.0 |
| `major` | Breaking changes, API removals, incompatible changes | 0.5.2 -> 1.0.0 |

For pre-stable projects (0.x.x), breaking changes are a minor bump. Never bump to 1.0.0 without explicit authorization.

### Description and context

- **description** (mandatory): A short summary of the release. Appears as a paragraph under the version heading in CHANGELOG.md and as the GitHub Release title suffix.
- **context** (optional): Multiline explanation of design decisions, rename rationale, or migration notes. Renders as a collapsible `<details>` block in CHANGELOG.md.

## Release pipeline order

The 18 steps execute in this exact order:

| Step | Action | Abort on failure |
| --- | --- | --- |
| 1 | Verify `gh` auth and clean working tree | Yes |
| 2 | Read `unreleased.toml` for bump type, description, context, and target selection | Yes |
| 3 | Validate JSONL changelog (all 10 checks) | Yes |
| 4 | Generate CHANGELOG.md from all JSONL files | Yes |
| 5 | Run `pre-checks.sh` hook | Yes |
| 6 | Run strictcli schema dump (`--dump-schema`) if project uses strictcli | Yes |
| 7 | Run `selfdoc gen --no-commit` if project uses selfdoc | Yes |
| 8 | Run selfdoc check (verify generated files are up-to-date) if project uses selfdoc | Yes |
| 9 | Run built-in tests (`uv run pytest` / `go test` / `npm test`) | Yes |
| 10 | Run built-in lint (library projects only) | Yes |
| 11 | Run `pre-release.sh` hook | Yes |
| 12 | Write new version to all detected target files + `.rlsbl/version` | Yes |
| 13 | Commit (message = tag string, e.g. `v1.2.3`), tag, push | Yes |
| 14 | Finalize JSONL: rename `unreleased.jsonl` to `x.y.z.jsonl` (chmod 444), create fresh `unreleased.jsonl`, regenerate CHANGELOG.md, generate `x.y.z.md`, commit | Yes |
| 15 | Create GitHub Release with the version's changelog section as notes | Yes |
| 16 | Upload assets if pipeline has `assets` or `custom_assets` configured | Yes |
| 17 | Run `post-release.sh` hook | No (non-fatal) |
| 18 | Print `Watch CI: rlsbl watch <sha>` | -- |

Steps 9 and 10 are conditionally skipped — see the hooks override mechanism below.

## Hooks

Three hook files in `.rlsbl/hooks/`:

| Hook | Runs at step | Ownership | Three-way merged on scaffold | Failure behavior |
| --- | --- | --- | --- | --- |
| `pre-checks.sh` | 5 | User-owned | No (created once, never touched again) | Non-zero aborts release |
| `pre-release.sh` | 11 | Scaffold-managed | Yes | Non-zero aborts release |
| `post-release.sh` | 17 | Scaffold-managed | Yes | Non-fatal (release continues) |

### Hooks override

When `pre-release.sh` has been customized — meaning its content hash does not match any known scaffold template version — steps 9 (built-in tests) and 10 (built-in lint) are skipped entirely. The assumption is that a customized pre-release hook handles testing and linting itself.

The override triggers when:
- The hook file exists AND its content differs from all known template versions (compared by SHA-256 hash with trailing whitespace stripped)

The override does NOT trigger when:
- The hook file is missing
- The hook file matches any known scaffold template version (including historical versions)

This means an unmodified scaffold hook or a missing hook file is considered "effectively empty" — built-in tests and lint run normally.

## Flags

| Flag | Effect |
| --- | --- |
| `--dry-run` | Preview the entire flow without making changes (no commits, tags, pushes, or GitHub Releases) |
| `--yes` | Non-interactive mode, skip all confirmation prompts |
| `--watch` | After release, automatically watch CI runs to completion |
| `--no-watch` | After release, print the watch command hint without watching |
| `--allow-dirty` | Skip the clean working tree check (step 1) |

`--dry-run`, `--yes`, and `--quiet` are global flags available on all rlsbl commands. `--allow-dirty`, `--watch`, and `--no-watch` are release-specific.

## Related commands

| Command | Purpose |
| --- | --- |
| `rlsbl release init` | Scaffold `.rlsbl/releases/unreleased.toml` with auto-detected targets |
| `rlsbl release retry` | Re-dispatch CI workflows for a completed release (reads from `retry.toml`) |
| `rlsbl release edit [version]` | Sync GitHub Release notes from CHANGELOG.md (defaults to current version) |
| `rlsbl release undo` | Revert last release: delete GitHub Release, delete tag, revert commit. Requires manual `git push` after. |
| `rlsbl release yank <version>` | Mark a past release as deprecated (`--hard` deletes entirely). Refuses to yank the latest release. |

## Dev node releases

Monorepo projects marked `dev_node = true` in `workspace.toml` have a simplified release flow:

- Changelog validation is skipped entirely (no `.rlsbl/changes/` directory, no `unreleased.jsonl`)
- CHANGELOG.md generation is skipped
- The `description` field in `unreleased.toml` is **mandatory** (it becomes the GitHub Release body)
- The release flow is: version bump, commit, tag, push, GitHub Release (with description + context as body)

Dev nodes are projects at the edge of the dependency graph — test infrastructure, conformance suites, dev tooling. Nothing user-facing depends on them. The `dev-node-boundary` check (`rlsbl check --tag workspace`) enforces this constraint.

## Source reference

:-: ref path="rlsbl.commands.release"
