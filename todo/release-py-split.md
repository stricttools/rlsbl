# Split release.py into a package

## Problem

`rlsbl/commands/release.py` is 1808 lines and contains 23 functions, 1 exception
class, and 5 module-level constants. Its central function `_run_release_mutating`
takes 24 parameters (12 positional + 12 keyword-only). The file mixes validation,
hook execution, version bumping, rollback, publishing, and orchestration into a
single module with no structural boundaries.

## Module decomposition

Split `rlsbl/commands/release.py` into `rlsbl/commands/release/` (a package) with
6 modules:

### orchestrate.py (~200 lines after extraction)

The orchestrator. Contains `run_cmd` (currently lines 568-1077, 510 lines) but
most of its body is validation logic that moves to `validate.py`, leaving the
orchestration skeleton at ~200 lines.

Also contains `VALID_BUMP_TYPES` (line 49).

Imports from: `validate`, `hooks`, `execute`

### validate.py

Target resolution, input validation, error types.

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `ReleaseAbortError` | 165-166 | Exception for unexpected dirty files during release |
| `parse_porcelain_paths` | 169-186 | Parse `git status --porcelain` output into path set |
| `resolve_target_paths` | 189-199 | Build dict of target name to resolved directory path |
| `resolve_release_targets` | 202-236 | Compute secondary targets for release (excludes primary) |
| `_abort_on_scaffold_conflicts` | 410-433 | Abort if scaffold files have unresolved conflict markers |
| `_rel_to_git_root` | 157-162 | Normalize path relative to git root |

Internal dependency: `resolve_release_targets` calls `resolve_target_paths` (both
in this module, no issue).

Imports from: no other release submodules (leaf module)

### hooks.py

Hook detection and built-in check runners.

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `_compute_content_hash` | 90-92 | SHA-256 of content (trailing whitespace stripped) |
| `_get_pre_release_template_hashes` | 99-134 | Frozenset of known scaffold template hashes (lazy-loaded) |
| `_is_hook_effectively_empty` | 137-154 | Check if pre-release hook matches scaffold template |
| `_run_builtin_tests` | 239-255 | Run tests; sys.exit(1) on failure |
| `_run_builtin_lint` | 258-294 | Run lint for library projects; sys.exit(1) on errors |
| `_run_selfdoc_gen` | 297-327 | Run `selfdoc gen --no-commit` |
| `_run_selfdoc_check` | 330-358 | Run `selfdoc check` |
| `_run_strictcli_schema_dump` | 436-473 | Run `--dump-schema` for strictcli projects |

Also contains constants: `_PRE_RELEASE_TEMPLATE_HASHES` (line 96),
`_SCHEMA_DUMP_TIMEOUT` (line 407).

Internal dependency: `_is_hook_effectively_empty` calls `_compute_content_hash`
and `_get_pre_release_template_hashes` (both in this module, no issue).

Imports from: no other release submodules (leaf module)

### execute.py

The mutating release phase and version-bump helpers.

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `_run_release_mutating` | 1242-1808 | Inner release logic: version bump, commit, tag, push, GitHub Release, subtree, deploy, post-release (567 lines) |
| `_bump_selfdoc_version` | 52-87 | Bump version in selfdoc.json |
| `_sync_lockfiles` | 486-548 | Re-sync uv.lock, package-lock.json, go.sum after bumps |
| `_refresh_selfdoc_hashes` | 361-404 | Re-run selfdoc check after version bump (non-fatal) |
| `_update_last_build_release` | 551-565 | Store last_build_release in config for OTA validation |

Also contains constants: `_LOCKFILE_SPECS` (lines 477-481),
`_LOCKFILE_SYNC_TIMEOUT` (line 483).

Imports from: `validate` (ReleaseAbortError, parse_porcelain_paths,
resolve_target_paths, _rel_to_git_root), `rollback` (_cleanup_release_artifacts),
`publish` (upload_release_assets, _print_stale_dep_advisory)

### rollback.py

Single function for post-failure cleanup.

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `_cleanup_release_artifacts` | 1219-1239 | Best-effort removal of orphaned generated files after rollback |

Imports from: no other release submodules (leaf module)

### publish.py

Asset upload and downstream dependency advisory.

| Symbol | Lines | Purpose |
|--------|-------|---------|
| `upload_release_assets` | 1124-1216 | Build and upload release assets for pipelines with assets config |
| `_print_stale_dep_advisory` | 1079-1121 | Print advisory about downstream packages with stale constraints |

Imports from: no other release submodules (leaf module)

## Dependency graph (proposed modules)

```
orchestrate --> validate
orchestrate --> hooks
orchestrate --> execute
execute     --> validate
execute     --> rollback
execute     --> publish
```

Leaf modules (no intra-package imports): `validate`, `hooks`, `rollback`, `publish`

No circular dependencies.

## ReleaseState dataclass

`_run_release_mutating` currently takes 24 parameters:

**12 positional:**

1. `registry` (str) -- target registry name (e.g. "npm", "pypi")
2. `reg` -- target instance (TARGETS[registry])
3. `flags` (dict) -- command flags (dry-run, yes, quiet, watch)
4. `quiet` (bool) -- suppress output
5. `log` (Callable) -- logging function
6. `new_version` (str) -- version being released
7. `current_version` (str) -- version before bump
8. `bump_type` (str | None) -- patch/minor/major or None for first release
9. `tag` (str) -- git tag (e.g. "v1.2.3")
10. `branch` (str) -- current git branch
11. `changelog_entry` (str | None) -- extracted markdown for GitHub Release notes
12. `target` -- target instance (same as `reg`, redundant)

**12 keyword-only:**

1. `secondary_targets` (dict[str, str] | None) -- name to path of secondary release targets
2. `monorepo_name` (str | None) -- project name in workspace
3. `monorepo_project_path` (str | None) -- project path relative to monorepo root
4. `commit_msg` (str | None) -- commit message for release commit
5. `primary_path` (str | None) -- resolved path for primary target
6. `target_paths` (dict[str, str] | None) -- all target name to path mappings
7. `lock_dir` (str) -- ".rlsbl" or ".rlsbl-monorepo"
8. `pre_existing_dirty` (set[str] | None) -- files dirty before release (from --allow-dirty)
9. `hook_generated` (set[str] | None) -- files created/modified by hooks
10. `description` (str) -- release description from release file
11. `context` (str) -- release context from release file
12. `ctx` (ProjectContext) -- project context (project_root, workspace_root, config)

### Proposed dataclass shape

```python
@dataclasses.dataclass
class ReleaseState:
    # --- identity ---
    registry: str
    target: Target           # TARGETS[registry] (replaces both `reg` and `target`)
    new_version: str
    current_version: str
    bump_type: str | None
    tag: str
    branch: str
    commit_msg: str

    # --- paths ---
    primary_path: str
    target_paths: dict[str, str]
    lock_dir: str

    # --- monorepo ---
    monorepo_name: str | None
    monorepo_project_path: str | None

    # --- metadata ---
    changelog_entry: str | None
    description: str
    context: str

    # --- state ---
    pre_existing_dirty: set[str]
    hook_generated: set[str]
    secondary_targets: dict[str, str] | None

    # --- control ---
    flags: dict
    quiet: bool
    log: Callable
    ctx: ProjectContext
```

Changes from current signature:
- `reg` and `target` merged into single `target` field (currently identical: both are `TARGETS[registry]`)
- `commit_msg` loses its None default (always computed before the call)
- `pre_existing_dirty` and `hook_generated` lose their None defaults (always set before the call)
- All 24 parameters collapse to 1

## 4-phase incremental migration

### Phase 1: file to package

Rename `release.py` to `release/_monolith.py`. Create `release/__init__.py` that
re-exports every public symbol:

```python
from ._monolith import (
    VALID_BUMP_TYPES,
    ReleaseAbortError,
    parse_porcelain_paths,
    resolve_target_paths,
    resolve_release_targets,
    run_cmd,
    upload_release_assets,
    _abort_on_scaffold_conflicts,
    _bump_selfdoc_version,
    _cleanup_release_artifacts,
    _compute_content_hash,
    _get_pre_release_template_hashes,
    _is_hook_effectively_empty,
    _print_stale_dep_advisory,
    _refresh_selfdoc_hashes,
    _rel_to_git_root,
    _run_builtin_lint,
    _run_builtin_tests,
    _run_release_mutating,
    _run_selfdoc_check,
    _run_selfdoc_gen,
    _run_strictcli_schema_dump,
    _sync_lockfiles,
    _update_last_build_release,
)
```

Zero test breakage: all 31 test files that `import from rlsbl.commands.release`
and all `patch("rlsbl.commands.release.X")` calls continue to work because
`__init__.py` re-exports everything.

### Phase 2: leaf extraction (in order)

Extract one module at a time. After each extraction, update `_monolith.py` imports
and `__init__.py` re-exports. Run tests after each step.

1. **rollback.py** (1 function, 20 lines). Simplest extraction -- no intra-module deps.

2. **publish.py** (2 functions, ~138 lines). No intra-module deps.

3. **validate.py** (1 class + 5 functions, ~82 lines). No intra-module deps.
   `resolve_release_targets` calls `resolve_target_paths` (both move together).

4. **hooks.py** (8 functions + 2 constants, ~170 lines). No intra-module deps.
   Internal call chain: `_is_hook_effectively_empty` -> `_compute_content_hash`,
   `_get_pre_release_template_hashes` -> `_compute_content_hash`.

5. **execute.py** (5 functions + 2 constants, ~700 lines). This is the big one.
   `_run_release_mutating` calls into validate, rollback, and publish. After
   extraction, `_monolith.py` is empty and can be deleted.

### Phase 3: orchestrator slimming

Extract validation logic from `run_cmd` body into named functions in
`validate.py`. The ~310 lines of validation checks in run_cmd (lines 585-885)
become function calls, leaving run_cmd as a ~200-line orchestration skeleton.

### Phase 4: ReleaseState dataclass

Replace the 24-parameter call to `_run_release_mutating` with a single
`ReleaseState` object. `run_cmd` populates the dataclass fields during its
orchestration, then passes a single object. This also makes it easy to serialize
release state for retry/resume.

## sys.exit audit

33 actual `sys.exit()` calls across the file.

### In run_cmd (25 calls) -- all should become exceptions

These are all validation/preparation checks. No mutations have occurred.
Replace with `ReleaseValidationError` (subclass of SystemExit or a new base).

| Line | Condition | Proposed replacement |
|------|-----------|---------------------|
| 587 | Empty include list in release file | ReleaseValidationError |
| 598 | Unknown target name in release file | ReleaseValidationError |
| 611 | Detected targets not in release file | ReleaseValidationError |
| 648 | OTA release but native files changed | ReleaseValidationError |
| 676 | "private" key missing from config | ConfigError (existing) |
| 692 | Private repo has local pipeline | ConfigError |
| 710 | Legacy 'publish' key in config | ConfigError |
| 717 | No 'pipelines' key in config | ConfigError |
| 731 | Missing env vars for local pipelines | ReleaseValidationError |
| 738 | gh CLI not installed | ReleaseValidationError |
| 741 | gh CLI not authenticated | ReleaseValidationError |
| 754 | Working tree not clean | ReleaseValidationError |
| 773 | Could not check remote-ahead status | ReleaseValidationError |
| 779 | Local branch behind origin | ReleaseValidationError |
| 792 | Inside monorepo but not in a project | ReleaseValidationError |
| 806 | dev_node project release attempt | ReleaseValidationError |
| 851 | Invalid bump type | ReleaseValidationError |
| 864 | Tag already exists | ReleaseValidationError |
| 871 | JSONL changelog not set up | ReleaseValidationError |
| 885 | JSONL validation failed | ReleaseValidationError |
| 907 | CHANGELOG.md not found after gen | ReleaseValidationError |
| 927 | Pre-checks hook non-zero exit | HookError |
| 930 | Pre-checks hook timeout | HookError |
| 970 | Pre-release hook non-zero exit | HookError |
| 973 | Pre-release hook timeout | HookError |

### In helper functions (4 calls) -- should become exceptions

| Line | Function | Condition | Proposed replacement |
|------|----------|-----------|---------------------|
| 254 | `_run_builtin_tests` | Tests failed | TestError |
| 285 | `_run_builtin_lint` | Lint errors | LintError |
| 326 | `_run_selfdoc_gen` | selfdoc gen failed | HookError |
| 357 | `_run_selfdoc_check` | selfdoc check failed | HookError |

### In upload_release_assets (1 call) -- should become exception

| Line | Function | Condition | Proposed replacement |
|------|----------|-----------|---------------------|
| 1205 | `upload_release_assets` | Asset exceeds max_asset_size_mb | ReleaseValidationError |

### In _abort_on_scaffold_conflicts (1 call) -- should become exception

| Line | Function | Condition | Proposed replacement |
|------|----------|-----------|---------------------|
| 433 | `_abort_on_scaffold_conflicts` | Unresolved merge conflict markers | ReleaseValidationError |

### In _run_release_mutating (4 calls) -- require per-site analysis

These fire after mutations have occurred. Each needs case-by-case analysis of
what has already happened and what rollback is needed.

| Line | Exit code | Condition | Analysis |
|------|-----------|-----------|----------|
| 1319 | 1 | User aborted (EOFError/KeyboardInterrupt) at confirmation | Pre-mutation (confirmation prompt). Safe to raise KeyboardInterrupt. |
| 1322 | 0 | User answered "n" at confirmation | Pre-mutation. Safe to return or raise UserDeclined. |
| 1595 | 1 | ReleaseAbortError caught (unexpected files after version bump) | Post-mutation (version bump committed). Rollback already attempted via `_cleanup_release_artifacts`. Must propagate to caller. ReleaseAbortError. |
| 1808 | 1 | release_created is False (GitHub Release creation failed) | Post-push. Tag and commits are on remote. Cannot roll back. Should raise ReleaseError with instructions. |

## Test impact

### Test files referencing rlsbl.commands.release: 31

| Test file | Symbols imported directly |
|-----------|--------------------------|
| test_hooks_override.py | `_compute_content_hash`, `_get_pre_release_template_hashes`, `_is_hook_effectively_empty`, `run_cmd` |
| test_release_selfdoc_ordering.py | `_refresh_selfdoc_hashes`, `_run_selfdoc_gen`, `run_cmd`, `_run_release_mutating` |
| test_release_lockfile_sync.py | `_sync_lockfiles` |
| test_release_rollback_cleanup.py | `_cleanup_release_artifacts` |
| test_release_builtin_checks.py | `_run_builtin_lint`, `_run_builtin_tests`, `_run_selfdoc_check`, `_run_selfdoc_gen`, `run_cmd` |
| test_release_assets.py | `upload_release_assets` |
| test_selfdoc_version_bump.py | `_bump_selfdoc_version` |
| test_deploy_integration.py | `_run_release_mutating` |
| test_deps_stale.py | `_print_stale_dep_advisory` |
| test_subdirectory_targets.py | `resolve_release_targets`, `resolve_target_paths` |
| test_scaffold_conflicts_check.py | `_abort_on_scaffold_conflicts` |
| test_commands.py | `run_cmd`, `parse_porcelain_paths`, `resolve_release_targets` |
| test_release_unexpected_files.py | `run_cmd` |
| test_monorepo_lock.py | `run_cmd` |
| test_release_abort_cleanup.py | `run_cmd` |
| test_release_allow_dirty.py | `run_cmd` |
| test_monorepo_release.py | `run_cmd` |
| test_strictcli_schema_release.py | `run_cmd`, `_run_strictcli_schema_dump` |
| test_release_file_integration.py | `run_cmd` |
| test_release_validated_cache.py | `run_cmd` |
| test_release_finalize_md.py | `run_cmd` |
| test_release_dirty_md.py | `run_cmd` |
| test_private_config.py | `run_cmd` |
| test_targets_cmd.py | `run_cmd` |
| test_release_watch.py | `run_cmd` |
| test_release_integration.py | `run_cmd` |
| test_release_hooks.py | `run_cmd` |
| test_dev_node.py | `run_cmd` (as `release_run_cmd`) |
| test_batch_release.py | (references via patch path) |
| test_release_retry.py | (references via patch path) |
| test_release_scrub.py | (references via patch path) |

### Distinct symbols imported: 18

`_abort_on_scaffold_conflicts`, `_bump_selfdoc_version`, `_cleanup_release_artifacts`,
`_compute_content_hash`, `_get_pre_release_template_hashes`, `_is_hook_effectively_empty`,
`_print_stale_dep_advisory`, `_refresh_selfdoc_hashes`, `_run_builtin_lint`,
`_run_builtin_tests`, `_run_release_mutating`, `_run_selfdoc_check`, `_run_selfdoc_gen`,
`_run_strictcli_schema_dump`, `_sync_lockfiles`, `parse_porcelain_paths`,
`resolve_release_targets`, `resolve_target_paths`, `run_cmd`, `upload_release_assets`

(20 distinct symbols: 18 functions + 1 class used only internally + run_cmd)

### Symbols patched via unittest.mock.patch: 31 distinct paths

These patch `rlsbl.commands.release.X` where X is either a symbol defined in
release.py or a symbol imported at module level (like `run`, `push_if_needed`,
`is_clean_tree`). The `__init__.py` re-export shim ensures all patch paths
continue to resolve during the migration.

### Zero test breakage guarantee

The `__init__.py` re-export shim in Phase 1 means:
- `from rlsbl.commands.release import X` continues to work
- `patch("rlsbl.commands.release.X")` continues to work
- No test file needs modification until the shim is removed (post-migration cleanup)

## Affected files

- `rlsbl/commands/release.py` (1808 lines) -- split into package
- 31 test files (listed above) -- no changes needed during migration (shim)
- `rlsbl/commands/__init__.py` -- no changes needed (already imports `release`)

## Effort estimate

- Phase 1 (file to package): 1 session
- Phase 2 (leaf extraction, 5 steps): 2-3 sessions
- Phase 3 (orchestrator slimming): 1 session
- Phase 4 (ReleaseState dataclass): 1 session
- Total: 5-6 sessions
