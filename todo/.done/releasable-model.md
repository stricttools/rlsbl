# Releasable model redesign

## Problem

Per-package versioning is waste for product monorepos. Of the 8 monorepos rlsbl manages, 6 do not need independent versioning -- all packages are private, share a lifecycle, and sit at 0.1.0 with empty changelogs. The current model forces:

- N copies of version fields, N changelogs, N tags for packages that share a lifecycle
- Linear scaling of release ceremony with package count (a 45-package monorepo turns a 15-minute release into hours)
- No monorepo-level identity or version -- there is no way to say "WWW is at v2.0"
- Convention-only lockstep ("always put all packages at the same bump") with no enforcement

The root cause is that "package" and "releasable unit" are conflated. They are the same thing in strictcli (3 packages, independent consumers), but not in WWW (50 packages, one product) or F (43 packages, one app).

## Decisions

### 1. Releasable = named group of packages sharing version/changelog/release

A **releasable** is the unit of versioning. Config on the project side: `releasable = "name"` to join a named group, `releasable = false` to opt out of versioning entirely. Directory: `.rlsbl-monorepo/releasables/{name}/` holds the version, changelog, and release files for each releasable.

### 2. One code path via implicit single-member releasables

Without `[[releasables]]` configuration, each package is implicitly its own single-member releasable. The internal data model is always releasable-based -- single-project repos and per-package monorepos use the same code path as multi-package releasables. No branching on "is this a releasable monorepo or a normal one."

### 3. Project-side membership (source of truth)

The `releasable = "name"` field on each project in `workspace.toml` is the source of truth. Member lists are derived by scanning projects, not maintained as an explicit list in a releasable config file. This avoids dual-source-of-truth problems and makes adding/removing members a one-line edit.

### 4. No catch-all releasable

When any project in the workspace uses `releasable = "name"` (explicit mode), every non-`dev_only` project must declare a releasable. Missing `releasable` in explicit mode is a hard error, not a silent default. This prevents packages from silently escaping the versioning system.

### 5. dev_node split into dev_only and releasable = false

The current `dev_node` flag conflates two independent concepts:

- **`dev_only`** (boundary guardrail): "nothing user-facing depends on this." Enforced by the dependency graph check -- a non-`dev_only` project cannot have a runtime dependency on a `dev_only` project.
- **`releasable = false`** (not versioned): "this project does not participate in the release system." No version, no changelog, no tags.

These become independent flags. `dev_only = true` + `releasable = "core"` is valid (a test infrastructure package that is part of the core releasable for versioning but excluded from the dependency boundary guardrail). `dev_only = false` + `releasable = false` is valid (a utility package consumed by others that is not independently versioned).

### 6. Version lives at releasable level

Version source of truth: `.rlsbl-monorepo/releasables/{name}/version`. `workspace.toml` is purely structural (project paths, dependencies, flags) -- no version fields. Each releasable owns its version independently.

### 7. No version injection for private packages

Private (unpublished) packages do not get version fields synced into their project files. Only published packages get version written during the release commit. This eliminates the ceremony of maintaining version fields in 40+ private packages that nobody reads.

### 8. Publishing config separated into publish.json

Publishing configuration (pipelines, targets) moves from `.rlsbl/config.json` to `.rlsbl/publish.json`. Hard error if both files contain publishing fields -- no silent fallback, no migration-period dual-read. This separates "how the project is structured" from "how the project is published."

### 9. Tag format configurable per releasable

Each releasable can configure its tag format (e.g., `v{version}`, `{name}@v{version}`, `{path}/v{version}`). Go packages are validated for compatibility with Go module versioning (`{path}/v{version}` required for modules at non-root paths).

### 10. Changelog per releasable with optional packages field

Each releasable has its own `unreleased.jsonl` at `.rlsbl-monorepo/releasables/{name}/changes/`. Entries gain an optional `packages` field listing which member packages are affected (derived from commit paths via watch patterns). The field is optional in the schema for backward compatibility -- entries without it are treated as affecting the entire releasable.

### 11. Cross-package commits allowed

A single commit can touch files in multiple packages (and therefore multiple releasables). This is normal in monorepos and must not be an error. Extraction via `git filter-repo` handles splitting history when a package moves between releasables.

### 12. Extract/absorb as first-class operations

Moving a package between releasables (or extracting to its own repo) is a supported operation with dedicated commands. History is preserved via `git filter-repo`. Changelog entries referencing the moved package are migrated.

### 13. Hooks at both per-releasable and per-package levels

Release hooks can exist at `.rlsbl-monorepo/releasables/{name}/hooks/` (releasable-level) and `.rlsbl/hooks/` (per-package). Both levels execute during release. Within each level, hooks run in alphabetical order. First failure aborts the release.

### 14. Release file uses [releasables.name] or [packages.name] sections

In explicit mode (releasables configured), the release file uses `[releasables.name]` sections with bump type and description per releasable. In implicit mode (each package is its own releasable), the release file uses `[packages.name]` sections, preserving the current format.

### 15. Unified model for single-project repos

Single-project repos (no `.rlsbl-monorepo/`) also use the releasable model internally. The project is an implicit single-member releasable. No new files are created -- the existing `.rlsbl/` directory structure is the releasable's storage. This means all release logic flows through one code path regardless of repo structure.

## Phases

### Phase 0: Foundation -- releasable data model
- 0a. Define `Releasable` dataclass (name, members, version_path, changes_dir, tag_format, hooks_dir)
- 0b. Implement `resolve_releasables()` that derives releasables from workspace.toml project entries
- 0c. Implement implicit single-member releasable for projects without explicit `releasable` field
- 0d. Implement implicit single-project releasable for non-monorepo repos
- 0e. Add `releasable` field to workspace.toml schema and parser

### Phase 1: Releasable directory structure
- 1a. Create `.rlsbl-monorepo/releasables/{name}/` directory layout
- 1b. Scaffold `changes/unreleased.jsonl` per releasable
- 1c. Scaffold `version` file per releasable
- 1d. Scaffold `hooks/` directory per releasable
- 1e. Update `rlsbl scaffold` to create releasable directories when configured

### Phase 2: dev_node split into dev_only + releasable
- 2a. Add `dev_only` field to workspace.toml schema
- 2b. Migrate boundary guardrail (`dev-node-boundary`) to use `dev_only` instead of `dev_node`
- 2c. Migrate `layers-violations` to use `dev_only` for filtering
- 2d. Migrate `test-suite-workspace` to use `dev_only` for filtering
- 2e. Migrate `changelog-coverage` dev_node gate to `releasable = false` check
- 2f. Migrate `changelog-user-facing` dev_node gate to `releasable = false` check
- 2g. Add backward-compat parser: `dev_node = true` implies `dev_only = true, releasable = false`
- 2h. Deprecation warning when `dev_node` is encountered in workspace.toml

### Phase 3: Version at releasable level
- 3a. Implement version read/write for releasable version files
- 3b. Migrate `version-consistency` to check releasable version, not per-package
- 3c. Migrate `target-version-readable` to check published packages only
- 3d. Migrate `selfdoc-version-drift` to use releasable version as reference
- 3e. Migrate `deps-stale` to use releasable version for cross-releasable dependency checks
- 3f. Implement version sync for published packages during release commit

### Phase 4: Changelog at releasable level
- 4a. Implement changelog read/write at releasable changes directory
- 4b. Add optional `packages` field to entry schema
- 4c. Derive `packages` from commit paths using watch patterns
- 4d. Migrate all 9 changelog checks to resolve via releasable, not per-package
- 4e. Migrate `prepush-changelog-coverage` to resolve via releasable
- 4f. Migrate `prepush-gitignore-guard` to include releasable directories
- 4g. Migrate `rlsbl changelog add` to target releasable's unreleased.jsonl
- 4h. Migrate `rlsbl changelog generate` to produce per-releasable CHANGELOG.md

### Phase 5: Publishing config separation
- 5a. Define `publish.json` schema (pipelines, targets, per-package publish overrides)
- 5b. Implement loader with dual-presence hard error
- 5c. Migrate `config-schema` check to validate both files
- 5d. Migrate `private-publish-workflow` to read from publish.json
- 5e. Migrate `npm-private-mismatch` to read from publish.json
- 5f. Migrate `prepush-manual-warning` to read release_branches from config.json (non-publish)
- 5g. Migration command: extract publishing fields from config.json into publish.json

### Phase 6: Release flow on releasable model
- 6a. Rewrite release file parser for `[releasables.name]` sections
- 6b. Rewrite `rlsbl release init` to scaffold per-releasable sections
- 6c. Rewrite `rlsbl release run` to iterate releasables, not packages
- 6d. Migrate `local-tag` check to use releasable tag format
- 6e. Migrate `remote-tag` check to use releasable tag format
- 6f. Migrate `github-release` check to use releasable tag format
- 6g. Rewrite `rlsbl monorepo release` to coordinate releasables
- 6h. Implement no-catch-all validation (hard error on missing releasable in explicit mode)

### Phase 7: Hooks at both levels
- 7a. Implement releasable-level hook discovery and execution
- 7b. Implement per-package hook execution within releasable release
- 7c. Define execution order: releasable pre-checks, per-package hooks (alphabetical), releasable pre-release
- 7d. Migrate `private-hook-stale` to check both levels

### Phase 8: Tag format configuration
- 8a. Add `tag_format` field to releasable config
- 8b. Implement Go module path validation for tag formats
- 8c. Update tag creation in release flow to use per-releasable format
- 8d. Update tag glob patterns in changelog range checks

### Phase 9: Extract/absorb operations
- 9a. Implement `rlsbl monorepo extract` (move package to own repo with history)
- 9b. Implement `rlsbl monorepo absorb` (bring external repo into workspace as package)
- 9c. Implement changelog entry migration during extract/absorb
- 9d. Implement `releasable` field update during extract/absorb

### Phase 10: Migration tooling
- 10a. `rlsbl migrate releasable` command for existing monorepos
- 10b. Migrate per-package changelog files to releasable changelog
- 10c. Migrate per-package version files to releasable version
- 10d. Validate migration completeness (no orphaned per-package state)

### Phase 11: Status and display updates
- 11a. Update `rlsbl status` to show releasable-grouped view
- 11b. Update `rlsbl monorepo graph` to annotate releasable membership
- 11c. Update `rlsbl monorepo snapshot` to include releasable state
- 11d. Update `rlsbl monorepo impact` to report per-releasable impact

### Phase 12: Cleanup
- 12a. Remove `dev_node` field support (after deprecation period)
- 12b. Remove per-package version/changelog code paths replaced by releasable model
- 12c. Update all error messages to reference releasable concepts
- 12d. Final audit: no code path should branch on "monorepo vs single-project" -- all goes through releasable

## Check Audit

26 of 50 checks need changes. Mapping to phases:

### Phase 2: dev_node split (5 checks)

| Check | Tag | Change |
|---|---|---|
| dev-node-boundary | workspace | Rename to `dev-only-boundary`, use `dev_only` flag |
| layers-violations | workspace | Filter by `dev_only` instead of `dev_node` |
| test-suite-workspace | workspace | Filter by `dev_only` instead of `dev_node` |
| changelog-coverage | changelog | Gate on `releasable = false` instead of `dev_node` |
| changelog-user-facing | changelog | Gate on `releasable = false` instead of `dev_node` |

### Phase 3: version (4 checks)

| Check | Tag | Change |
|---|---|---|
| version-consistency | project | Compare against releasable version, not per-package |
| target-version-readable | project | Only check published packages |
| selfdoc-version-drift | project | Use releasable version as reference |
| deps-stale | workspace | Use releasable version for cross-releasable deps |

### Phase 4: changelog (11 checks)

| Check | Tag | Change |
|---|---|---|
| changelog-entry | changelog | Resolve via releasable's CHANGELOG.md |
| changelog-hashes | changelog | Read from releasable's changes dir |
| changelog-range | changelog | Use releasable's tag glob |
| changelog-coverage | changelog | Read from releasable's changes dir, scope by member packages |
| changelog-orphans | changelog | Read from releasable's changes dir |
| changelog-schema | changelog | Read from releasable's changes dir, validate `packages` field |
| changelog-user-facing | changelog | Read from releasable's changes dir |
| changelog-batch-commits | changelog | Read from releasable's changes dir |
| changelog-batch-entries | changelog | Read from releasable's changes dir |
| prepush-changelog-coverage | prepush | Resolve affected releasable, check its changelog |
| prepush-gitignore-guard | prepush | Include releasable directories in guard |

### Phase 5: publishing (4 checks)

| Check | Tag | Change |
|---|---|---|
| config-schema | project | Validate publish.json separately, dual-presence error |
| private-publish-workflow | project | Read from publish.json |
| npm-private-mismatch | project | Read from publish.json |
| prepush-manual-warning | prepush | Read release_branches from config.json (unchanged source, but verify no publish.json dependency) |

### Phase 6: release (3 checks)

| Check | Tag | Change |
|---|---|---|
| local-tag | release | Use releasable tag format |
| remote-tag | release | Use releasable tag format |
| github-release | release | Use releasable tag format |

### Phase 7: hooks (1 check)

| Check | Tag | Change |
|---|---|---|
| private-hook-stale | project | Check both releasable-level and per-package hooks |

### No change needed (24 checks)

| Check | Tag | Reason |
|---|---|---|
| lock | project | Per-package, unrelated to versioning |
| name-consistency | project | Per-package metadata |
| license-consistency | project | Per-package metadata |
| description-consistency | project | Per-package metadata |
| license-file | project | Per-package metadata |
| scaffold-conflicts | project | Per-package scaffolding |
| branch-sync | release | Branch-level, not package-level |
| library-lint | quality | Per-package, unrelated to releasable |
| dead-modules | quality | Per-package source analysis |
| circular-deps | quality | Per-package source analysis |
| scaffold-unreplaced-vars | quality | Per-package scaffolding |
| test-suite | quality | Per-package test runner |
| workspace-ci-router | workspace | CI infrastructure |
| workspace-ci-synced | workspace | CI infrastructure |
| workspace-targets | workspace | Per-package target detection |
| workspace-unregistered | workspace | Directory scanning |
| workspace-stale-entries | workspace | Directory scanning |
| dead-workspace-packages | workspace | Directory scanning |
| subtree-remote-reachable | workspace | Subtree infrastructure |
| workspace-unbuildable | workspace | Build validation |
| deps-unused | workspace | Per-package import analysis |
| deps-undeclared | workspace | Per-package import analysis |
| deps-runtime-test-only | workspace | Per-package import analysis |
| deps-dev-in-lib | workspace | Per-package import analysis |

## dev_node Migration Table

130 lines across 22 files (19 source, 11 test files with overlap), split into three categories:

### Releasable membership (26 source + ~40 test lines)

Gates that check "can this project produce releases?" -- these become `releasable = false` checks.

| File | Lines | What it does |
|---|---|---|
| `rlsbl/commands/release/validate.py` | 8 | `resolve_monorepo_context` blocks release for dev_node |
| `rlsbl/commands/release/__init__.py` | 1 | Receives `is_dev_node` from validate |
| `rlsbl/commands/edit_release.py` | 5 | Blocks `release edit` for dev_node |
| `rlsbl/commands/changelog_cmd.py` | 2 | Blocks `changelog add` for dev_node |
| `rlsbl/commands/unreleased.py` | 4 | Skips/annotates unreleased commit count |
| `rlsbl/commands/monorepo/batch_release.py` | 5 | Rejects dev_node from batch release |
| `rlsbl/commands/monorepo/batch_release_init.py` | 3 | Skips dev_node in release init scaffolding |
| `rlsbl/commands/init_cmd.py` | 5 | `_is_dev_node_project()` skips changelog scaffold |
| `rlsbl/checks/changelog.py` | 4 | Skips coverage and user-facing checks |
| `rlsbl/checks/prepush.py` | 2 | Skips changelog coverage for dev_node |
| `rlsbl/commands/release_scrub.py` | 2 | Skips dev_node in release scrub |

### dev_only -- boundary guardrail (12 source + ~35 test lines)

Boundary guardrail, dependency graph, layers, test filtering, CLI flag. These become `dev_only` checks.

| File | Lines | What it does |
|---|---|---|
| `rlsbl/checks/workspace.py` | 10 | `check_dev_node_boundary` enforces no runtime deps on dev_node |
| `rlsbl/checks/workspace.py` | 2 | `check_layers_violations` filters dev_node from layer checks |
| `rlsbl/checks/workspace.py` | 2 | `check_test_suite_workspace` filters dev_node |
| `rlsbl/dep_validation.py` | 3 | Skips dev_node in dep validation |
| `rlsbl/workspace.py` | 2 | `dev_node` property on WorkspaceProject |
| `rlsbl/commands/monorepo/commands.py` | 8 | `--dev-node` CLI flag on `monorepo add` |
| `rlsbl/__init__.py` | 3 | Passes `dev_node` flag through to monorepo add |

### Display (8 source + ~5 test lines)

Status, snapshot, graph JSON output.

| File | Lines | What it does |
|---|---|---|
| `rlsbl/commands/status.py` | 2 | Shows "dev node -- no changelog" in status |
| `rlsbl/snapshot.py` | 1 | Includes `dev_node` in snapshot dict |
| `rlsbl/commands/monorepo/graph.py` | 3 | Includes `dev_node` in graph JSON and display |
| `rlsbl/commands/monorepo/commands.py` | 6 | Shows `dev_node` column in monorepo status table |

## Affected files

Core model: `rlsbl/workspace.py`, `rlsbl/check_context.py`

Checks: all 6 files in `rlsbl/checks/`

Commands: `rlsbl/commands/release/`, `rlsbl/commands/monorepo/`, `rlsbl/commands/changelog_cmd.py`, `rlsbl/commands/edit_release.py`, `rlsbl/commands/init_cmd.py`, `rlsbl/commands/status.py`, `rlsbl/commands/unreleased.py`, `rlsbl/commands/release_scrub.py`

Changelog: `rlsbl/changelog/files.py`, `rlsbl/changelog/validate.py`

Supporting: `rlsbl/dep_validation.py`, `rlsbl/snapshot.py`, `rlsbl/__init__.py`

Tests: 11 test files with `dev_node` references, plus new tests for releasable model

## Effort estimate

Large. 13 phases, ~50 subphases. Phases 0-4 are the critical path (data model, dev_node split, version, changelog). Phases 5-8 can partially parallelize. Phases 9-12 are follow-up. Estimated 15-25 sessions depending on test coverage depth.
