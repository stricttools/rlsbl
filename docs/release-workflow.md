---
description: "Complete reference for the rlsbl release flow — prerequisites, pipeline order, hooks, flags, dev node behavior, and related commands."
---

# Release workflow

## Overview

`rlsbl release run` orchestrates the full release lifecycle: validates the project state, bumps the version, runs quality checks, commits, tags, pushes, creates a GitHub Release, finalizes the changelog, and optionally watches CI. The entire flow is driven by a release file (`.rlsbl/releases/unreleased.toml`) that declares the bump type, description, and optional context.

Validation failures abort with no partial state left behind. Once the mutating phase starts, every step records a success or failure marker in an in-progress state file: a fatal failure preserves the state so `rlsbl release resume` can continue from where the release stopped, and non-fatal failures are recorded and loudly named in the completion summary while the release completes (see "Resumable release state" below). Use `rlsbl release undo` to revert a release that succeeded locally but failed in CI.

## Prerequisites

Before running `rlsbl release run`, the project must satisfy several preconditions. Each is enforced as a hard error at the start of the release flow — the release aborts immediately with a clear message indicating which requirement failed and how to fix it. Addressing these upfront avoids partial releases that need manual cleanup.

| Requirement | How to verify | What happens if missing |
| --- | --- | --- |
| Clean working tree | `git status --porcelain` is empty | Hard error (use `--allow-dirty` to override) |
| `gh` CLI authenticated | `gh auth status` | Hard error |
| Changelog coverage | `rlsbl check --tag changelog` passes | Hard error during validation step |
| Release file exists | `.rlsbl/releases/unreleased.toml` present | Hard error (run `rlsbl release init`) |
| Description set | `description` field in unreleased.toml is non-empty | Hard error |

## The release file

The release file at `.rlsbl/releases/unreleased.toml` drives the entire release flow. Scaffold it with `rlsbl release init`, which auto-detects targets, sets a default bump type of `patch`, and generates a template with placeholder fields for description and context:

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

### Per-target configuration sections

Some targets require additional configuration in the release file via `[targets.<name>]` sections, providing target-specific metadata that cannot be inferred from the project's manifest. Currently, this applies only to the Flutter target, which needs a deployment mode declaration to distinguish OTA updates from full app store builds.

**Flutter target** requires a `[targets.flutter]` section with a `mode` field. Valid modes:

| Mode | Description |
| --- | --- |
| `ota` | Over-the-air update (code push without a full app store rebuild) |
| `build` | Full build release (triggers app store build pipeline) |

If a Flutter target is listed in `include` but has no corresponding `[targets.flutter]` section with a `mode` field, the release file validation fails with a hard error.

Example with Flutter per-target config:

```toml
# .rlsbl/releases/unreleased.toml
bump = "minor"
description = "Add offline sync support"
include = ["flutter"]
exclude = []

[targets.flutter]
mode = "build"
```

`rlsbl release init` auto-generates the `[targets.flutter]` section with `mode = "build"` as the default when a Flutter target is detected. Change the mode before running `rlsbl release run` if an OTA release is intended.

Target config sections for targets not listed in `include` are rejected as validation errors. Only fields documented for a target type are allowed — unknown fields cause a hard error.

## Release pipeline order

The release pipeline executes its steps in a fixed order, from initial validation through post-release hooks. Validation steps abort with no partial state left behind; once the mutating phase starts, progress is tracked in an in-progress state file so a failed release can be resumed with `rlsbl release resume`. Steps 9 and 10 are conditionally skipped when the pre-release hook is customized:

| Step | Action | Abort on failure |
| --- | --- | --- |
| 1 | Verify `gh` auth and clean working tree | Yes |
| 2 | Read `unreleased.toml` for bump type, description, context, and target selection | Yes |
| 3 | Validate JSONL changelog (all 9 checks) | Yes |
| 4 | Generate CHANGELOG.md from all JSONL files | Yes |
| 5 | Run `pre-checks.sh` hook | Yes |
| 6 | Run strictcli schema dump (`--dump-schema`) if project uses strictcli | Yes |
| 7 | Run `selfdoc gen --no-auto-commit` if project uses selfdoc | Yes |
| 8 | Run selfdoc check (verify generated files are up-to-date) if project uses selfdoc | Yes |
| 9 | Run built-in tests (`uv run pytest` / `go test` / `npm test`) | Yes |
| 10 | Run built-in lint (library projects only) | Yes |
| 11 | Run `pre-release.sh` hook | Yes |
| 12 | Write new version to all detected target files + `.rlsbl/version` | Yes |
| 13 | Commit (message = tag string, e.g. `v1.2.3`), tag, push | Yes |
| 14 | Finalize JSONL: rename `unreleased.jsonl` to `x.y.z.jsonl` (chmod 444), create fresh `unreleased.jsonl`, regenerate CHANGELOG.md, generate `x.y.z.md`, commit | Yes |
| 15 | Create GitHub Release with the version's changelog section as notes | Yes |
| 16 | Upload assets if pipeline has `assets` or `custom_assets` configured | Yes (state preserved, resumable) |
| 17 | Run pipeline `publish` for each configured pipeline (skipped for `private: true`) | Yes (state preserved, resumable) |
| 18 | Deploy configured targets | No (failure recorded and named in the completion summary) |
| 19 | Run `post-release.sh` hook | No (failure recorded and named in the completion summary) |
| 20 | Regenerate monorepo snapshot | No (failure recorded and named in the completion summary) |
| 21 | Print `Watch CI: rlsbl watch <sha>` | -- |

Steps 9 and 10 are conditionally skipped — see the hooks override mechanism below.

### Release state and resume

From the version bump onward, every step records a success or failure marker in an in-progress state file (`.rlsbl/releases/in-progress.json`; for releasable releases, `.rlsbl-monorepo/releasables/<name>/releases/in-progress.json`). If a fatal step fails (anything through pipeline publish), the state file is preserved and `rlsbl release resume` continues from where the release stopped, skipping already-completed steps — including post-release steps such as asset upload. Non-fatal failures (deploy, post-release hook, snapshot) are recorded and loudly named in the completion summary, and the release completes. The state file is cleared only when every step carries a marker and no fatal step failed; `rlsbl release run` auto-clears a provably-complete leftover state file instead of blocking.

## Hooks

Three shell scripts in `.rlsbl/hooks/` provide extension points at different stages of the release pipeline. Each hook runs in the project root directory with the new version available as `$RLSBL_VERSION`. A non-zero exit code from `pre-checks.sh` or `pre-release.sh` aborts the release immediately, while `post-release.sh` failures are logged but do not roll back the already-published release.

| Hook | Runs at step | Ownership | Three-way merged on scaffold | Failure behavior |
| --- | --- | --- | --- | --- |
| `pre-checks.sh` | 5 | User-owned | No (created once, never touched again) | Non-zero aborts release |
| `pre-release.sh` | 11 | Scaffold-managed | Yes | Non-zero aborts release |
| `post-release.sh` | 19 | Scaffold-managed | Yes | Non-fatal (release continues) |

### Hooks override

When `pre-release.sh` has been customized — meaning its content hash does not match any known scaffold template version — steps 9 (built-in tests) and 10 (built-in lint) are skipped entirely. The assumption is that a customized pre-release hook handles testing and linting itself.

The override triggers when:
- The hook file exists AND its content differs from all known template versions (compared by SHA-256 hash with trailing whitespace stripped)

The override does NOT trigger when:
- The hook file is missing
- The hook file matches any known scaffold template version (including historical versions)

This means an unmodified scaffold hook or a missing hook file is considered "effectively empty" — built-in tests and lint run normally.

## Flags

`rlsbl release run` accepts both global flags (shared with all rlsbl commands) and release-specific flags that control working tree validation and post-release CI monitoring. The `--watch` and `--no-watch` flags are mutually exclusive — exactly one must be specified when running non-interactively with `--yes`.

| Flag | Effect |
| --- | --- |
| `--dry-run` | Preview the entire flow without making changes (no commits, tags, pushes, or GitHub Releases) |
| `--yes` | Non-interactive mode, skip all confirmation prompts |
| `--watch` | After release, automatically watch CI runs to completion |
| `--no-watch` | After release, print the watch command hint without watching |
| `--allow-dirty` | Skip the clean working tree check (step 1) |

`--dry-run`, `--yes`, and `--quiet` are global flags available on all rlsbl commands. `--allow-dirty`, `--watch`, and `--no-watch` are release-specific.

## Related commands

The `release` command group provides 6 subcommands covering the full release lifecycle — from scaffolding the release file through post-release corrections and rollbacks. Each subcommand is designed for a specific phase: `init` prepares, `run` executes, `retry` recovers from CI failures, `edit` corrects release notes, `undo` reverts a bad release, and `yank` deprecates old versions.

| Command | Purpose |
| --- | --- |
| `rlsbl release init` | Scaffold `.rlsbl/releases/unreleased.toml` with auto-detected targets |
| `rlsbl release retry` | Re-dispatch CI workflows for a completed release (reads from `retry.toml`) |
| `rlsbl release edit [version]` | Sync GitHub Release notes from CHANGELOG.md (defaults to current version) |
| `rlsbl release undo` | Revert last release: delete GitHub Release, delete tag, revert commit. Requires manual `git push` after. |
| `rlsbl release yank <version>` | Mark a past release as deprecated (`--hard` deletes entirely). Refuses to yank the latest release. |

## Dev node projects

Dev nodes are projects at the edge of the dependency graph that nothing user-facing depends on — test infrastructure, conformance suites, dev tooling, and internal utilities consumed only during development. Dev nodes cannot be released:

- **No changelog system**: no `.rlsbl/changes/`, no `unreleased.jsonl`, no `CHANGELOG.md`
- **No releases**: `rlsbl release run` and `rlsbl release edit` error with "dev_node projects cannot be released"
- `rlsbl changelog add` errors with "dev node projects don't use changelogs"
- Scaffold skips changelog infrastructure
- Pre-push check ignores dev node commits
- Batch release (`rlsbl monorepo release run`) excludes dev nodes
- Remove `dev_node = true` from workspace.toml to make a project releasable
- The `dev-only-boundary` check prevents non-dev-node projects from declaring runtime dependencies on dev nodes

## Scrubbing sensitive content

When sensitive content is discovered in git history (credentials, confidential project names, etc.), `rlsbl release scrub` wraps safegit's history rewriting with automatic release metadata cleanup. Rewriting history changes every commit SHA from the rewrite point forward, which would normally break the JSONL changelog's hash references, the validation cache, and existing GitHub Releases — so the command repairs all of that release metadata in one pass.

Usage:
```
rlsbl release scrub --pattern "secret_token_.*" --replace "REDACTED" --reason "Remove leaked API keys" --entire-history
rlsbl release scrub --file config/secrets.yml --mangle --reason "Remove secrets file" --from v0.50.0
```

The command:
1. Runs safegit scrub (match or file mode) to rewrite git history
2. Remaps commit hashes in all JSONL changelog files using the old-to-new SHA mapping
3. Regenerates CHANGELOG.md from updated JSONL
4. Invalidates validation caches
5. Commits the metadata updates
6. Force-pushes the branch (--force-with-lease) and affected tags (--force)
7. Recreates GitHub Releases for affected tags with updated changelog notes

Flags: `--pattern` or `--file` (what to scrub), `--replace` or `--mangle` (replacement strategy), `--reason` (required, appears in commit message), `--from` or `--entire-history` (scope).

Error recovery: if the command fails partway, `scrub-result.json` preserves the safegit output at `.rlsbl/releases/scrub-result.json` (for releasable releases: `.rlsbl-monorepo/releasables/<name>/releases/scrub-result.json`). Re-running the command resumes from the last completed step without re-running safegit.

Requires safegit 0.18.0+ (for --json output).

## Source reference

The release workflow is implemented in the `rlsbl.commands.release` module, which orchestrates the full release pipeline (see the step table above) from validation through GitHub Release creation and the post-release phase. This module coordinates version bumping, JSONL finalization, git operations, and hook execution.

:-: ref path="rlsbl.commands.release"
