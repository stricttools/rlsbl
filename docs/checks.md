---
description: "Reference for rlsbl checks across 6 tags, covering check metadata, severity levels, target applicability, and workspace CI-router sync verification."
---

# Check system

:-: check-count

Run checks via the `rlsbl check` command. Checks are organized across 6 primary tags (project, release, changelog, workspace, quality, prepush) and validate project metadata, release state, changelog structure, workspace integrity, code quality, and pre-push enforcement. Four additional untagged checks run only with `--all` or `--name`. Three further tags exist for pipeline and target-specific grouping: `preflight` and `preflight-changelog` are run internally by `rlsbl release run`, and `maven` groups the Maven-specific check.

## Running checks

```bash
# Run all checks
rlsbl check --all

# Run all checks in a tag
rlsbl check --tag changelog

# Run a single check by name
rlsbl check --name version-consistency
```

## Check results

Each check returns one of four statuses that determine how the result is displayed and whether it blocks the release pipeline. The severity level (error or warn) is declared per-check in the check metadata stored in `checks.toml` and controls which status is reported on failure versus advisory findings:

| Status | Meaning | Effect |
| --- | --- | --- |
| pass | Check passed | No action needed |
| fail | Check failed | Blocking error -- must be fixed before release |
| warn | Advisory finding | Informational -- does not block release |
| skip | Not applicable | Check cannot run for this project (e.g., workspace check in a standalone project) |

Severity is declared per-check in metadata. A check with `severity = "error"` reports `fail` on failure; one with `severity = "warn"` reports `warn`.

## Tags

| Tag | Purpose | Check count |
| --- | --- | --- |
| `project` | Project-level metadata, config schema, version consistency | 21 |
| `release` | Git tag and GitHub Release validation | 5 |
| `changelog` | JSONL changelog validation and structure | 11 |
| `workspace` | Monorepo workspace integrity and dependency rules | 17 |
| `quality` | Code quality, dependency analysis, scaffold hygiene | 10 |
| `prepush` | Pre-push enforcement: changelog coverage, gitignore guard, manual-push warning, tests | 6 |

Some checks carry multiple tags, so they appear in multiple tag counts: `test-suite` is tagged `prepush` and `quality`, `test-suite-workspace` is tagged `prepush` and `workspace`, and `scaffold-conflicts` is tagged `project`, `prepush`, and `release`. Four checks (`layers-violations`, `deps-unused`, `deps-undeclared`, `deps-stale`) have no tag and only run with `--all` or `--name`. Internal tags used by the release pipeline: `preflight` (8 checks: `library-lint`, `test-suite`, `dev-overlay-drift`, `maven-central-metadata`, `wrapper-producer`, `strictspec-certificate-gate`, `stricttest-floor`, `dep-floors`) and `preflight-changelog` (9 checks: the structural changelog checks, i.e. all changelog checks except `changelog-entry` and `changelog-format-version`). The `maven` tag groups `maven-central-metadata`.

## Project checks

| Check | Severity | Description |
| --- | --- | --- |
| `lock` | warn | Detects stale lock state in `.rlsbl/` |
| `version-consistency` | error | Project version matches across all target files (e.g., `pyproject.toml`, `package.json`, `.rlsbl/version`) |
| `name-consistency` | warn | Package name is consistent across manifest files |
| `description-consistency` | warn | Package description is consistent across manifest files |
| `license-file` | error | A LICENSE file exists in the project root |
| `license-consistency` | warn | License identifier matches across manifest files |
| `config-schema` | error | `.rlsbl/config.json` conforms to the expected schema (no unknown keys, correct types) |
| `private-hook-stale` | error | Detects leftover private repo hook files that should be deleted |
| `publish-mode-workflow` | error | `publish_mode: "none"` repos must not have a publish workflow that pushes to public registries |
| `npm-private-mismatch` | error | `package.json` private field matches `.rlsbl/config.json` private flag (npm targets only) |
| `target-version-readable` | error | Version can be read from all declared target files |
| `dunder-version-missing` | error | PyPI targets that keep a version constant in source must use `__version__` |
| `selfdoc-version-drift` | error | selfdoc-generated version references match the actual project version |
| `scaffold-conflicts` | error | Unresolved git merge conflict markers in scaffold files (managed-files registry, `.github/workflows/`, all of `.rlsbl/`); also tagged `prepush` and `release` |
| `cross-repo-path-sources` | error | `[tool.uv.sources]` path entries in the committed `pyproject.toml` must resolve inside the repository (in-repo paths and `workspace = true` are legal; local overrides belong in `dev-sources.toml.local-only`). Also enforced unconditionally by `rlsbl release run` |
| `dev-overlay-drift` | error | Packages recorded in the `rlsbl dev sync` sentinel are still editable installs of their declared checkouts (a bare `uv sync` silently replaces an overlay with the released wheel) |
| `requires-services` | error | CI service containers declared under `services`/`test_env` are actually provisioned in the rendered CI workflow |
| `wrapper-producer` | error | Every launcher pipeline's `wraps` reference names a real binary-artifact pipeline, and the wrapped target's manifest still carries the shim-critical fields |
| `strictspec-certificate-gate` | error | A configured strictspec diff certificate reports no violated (or unsupported-and-unadjudicated) claim. Skips when the project has no `strictspec_gate` section |
| `stricttest-floor` | error | An adopted sandboxed test runner works: the `test_sandbox` runner script exists and is executable, the config family is complete, and every CI workflow the family names actually invokes the runner. Skips when the project has adopted neither the `test_sandbox` family nor the stricttest plugin |
| `dep-floors` | error | Ecosystem-internal dependencies declare a `>=` floor at the version the lock resolves. Compares `pyproject.toml` against `uv.lock` and `package.json` against `package-lock.json`; Go is structurally satisfied (`require` lines are the minimums). Skips when the project has no `internal_dep_floors` config key |

## Release checks

| Check | Severity | Description |
| --- | --- | --- |
| `local-tag` | warn | A git tag exists locally for the current version |
| `remote-tag` | warn | The version tag has been pushed to the remote (requires network) |
| `github-release` | warn | A GitHub Release exists for the current version tag (requires network) |
| `branch-sync` | error | Local branch is not behind the remote tracking branch (requires network) |

`scaffold-conflicts` (see project checks) is also tagged `release`. Release checks form a dependency chain: `version-consistency` -> `local-tag` -> `remote-tag` -> `github-release`. If an upstream check fails, downstream checks are skipped.

## Changelog checks

| Check | Severity | Description |
| --- | --- | --- |
| `changelog-hashes` | error | Every commit hash in JSONL entries resolves via `git rev-parse` |
| `changelog-range` | error | Every resolved hash falls within the unreleased range (after the last version tag) |
| `changelog-coverage` | error | Every unreleased commit appears in at least one JSONL entry |
| `changelog-orphans` | error | No entries where ALL hashes are unresolvable (stale from rebased/amended commits) |
| `changelog-schema` | error | User-facing entries have `description` and `type`; type is one of `feature`/`fix`/`breaking` |
| `changelog-user-facing` | warn | At least one entry is user-facing (hard error during release, warning in check mode) |
| `changelog-batch-commits` | error | No single entry references more commits than `max_commits_per_entry` (default 5) |
| `changelog-batch-entries` | error | No single commit appears in more entries than `max_entries_per_commit` (default 5) |
| `changelog-entry` | warn | `CHANGELOG.md` contains an entry for the current project version |
| `changelog-format-version` | warn | The repo has recorded a `changelog_format_version_enforced` decision (enabling the gate below, or staying in legacy mode deliberately) |
| `changelog-format-version-gate` | error | When enforcement is on, every line in `unreleased.jsonl` and every finalized `x.y.z.jsonl` carries a supported `format_version`. Skipped while enforcement is off |

Dependencies: `changelog-range` and `changelog-coverage` depend on `changelog-hashes` (hash resolution must succeed first).

## Workspace checks

| Check | Severity | Description |
| --- | --- | --- |
| `workspace-ci-router` | error | The generated `ci-router.yml` exists at the repo root (it holds every project's inlined jobs; per-project coverage is `workspace-ci-synced`) |
| `workspace-ci-synced` | error | Each in-scope project's CI jobs are inlined into the shared `ci-router.yml` |
| `workspace-targets` | error | Each project's declared target matches its actual manifest files |
| `workspace-unregistered` | error | No project directories with manifest files exist outside of `workspace.toml` |
| `workspace-stale-entries` | error | No `workspace.toml` entries point to directories that no longer exist |
| `dev-only-boundary` | error | No non-dev-only project has a runtime dependency on a dev-only project |
| `unversioned-boundary` | error | No releasable project has a runtime dependency on an unversioned project (`releasable = false`, not dev-only) |
| `dead-workspace-packages` | warn | Detects workspace packages with no commits since their last release |
| `subtree-remote-reachable` | error | Configured subtree remote URLs are reachable (requires network) |
| `workspace-unbuildable` | error | Workspace members build under `uv sync --all-packages` (pypi workspaces only) |
| `scaffold-gitignore-stale` | warn | Workspace project `.gitignore` files contain all rlsbl-managed entries |
| `root-rlsbl-conflict` | error | Root `.rlsbl/` does not coexist with `.rlsbl-monorepo/` |
| `go-companion-tags` | warn | Non-private Go members of releasables have companion tags for the current version; a broken member config is a hard failure |
| `releasable-residue` | error | Releasable member packages carry no per-package release state (`.rlsbl/changes/`, `.rlsbl/releases/`, `.rlsbl/version`, etc.); `hooks/` and root-path members are exempt |
| `member-pytest-config` | error | When the workspace root has a `conftest.py`, every member with a `tests/` directory pins its own pytest rootdir, so a member run cannot escape into the root config |
| `mixed-tag-schemes` | error | No member directory declares both Go's path-based `{path}/v*` tags and `{name}@v*` tags, which would make the publish-router prefix ordering-dependent |

`test-suite-workspace` (see prepush checks) is also tagged `workspace`.

## Quality checks

| Check | Severity | Description |
| --- | --- | --- |
| `dead-modules` | warn | Detects source modules with no inbound imports (unreachable code) |
| `dead-modules-stale` | error | Every path declared in `dead-modules.toml` still exists, so an exclusion cannot silently outlive the file it excused |
| `circular-deps` | warn | Detects circular import dependencies between modules |
| `library-lint` | error | Runs lint rules for library projects (API surface, exports) |
| `ruff-lint` | error | Project passes ruff lint checks (skipped when ruff is not installed) |
| `deps-runtime-test-only` | warn | Runtime dependencies that are only imported in test files |
| `deps-dev-in-lib` | error | Dev dependencies used in library source (should be runtime deps) |
| `scaffold-unreplaced-vars` | error | Leftover `{{...}}` template placeholders in workflow files |
| `maven-central-metadata` | error | Maven Central publishing requirements are met (POM metadata, sources/javadoc jars); also tagged `maven` |

`test-suite` (see prepush checks) is also tagged `quality`.

## Prepush checks

| Check | Severity | Description |
| --- | --- | --- |
| `prepush-changelog-coverage` | error | Verifies every pushed commit has a JSONL changelog entry |
| `prepush-gitignore-guard` | error | Blocks push if rlsbl-managed files are gitignored |
| `prepush-manual-warning` | warn | Warns on manual push to release branch (non-blocking) |
| `test-suite` | error | Runs project tests (`pytest` / `go test` / `npm test`) |
| `test-suite-workspace` | error | Runs tests for affected workspace projects (monorepo only) |

`scaffold-conflicts` (see project checks) is also tagged `prepush`. Dependencies: `test-suite` and `test-suite-workspace` both depend on `prepush-changelog-coverage` -- fast checks fail first, so the test suite is skipped if changelog coverage fails. `test-suite` is also tagged `quality`, so it runs under both `rlsbl check --tag prepush` and `rlsbl check --tag quality`.

## Untagged checks

These 4 checks have no tag assignment and run only when explicitly requested via `--all` or `--name`. They are excluded from tag-based runs because they require specific project configurations (layer rules, workspace manifests) or have longer execution times:

| Check | Severity | Description |
| --- | --- | --- |
| `layers-violations` | error | Dependency direction violates architectural layer rules defined in `workspace.toml` |
| `deps-unused` | error | Declared dependencies that are never imported |
| `deps-undeclared` | error | Imported packages that are not declared as dependencies |
| `deps-stale` | error | Workspace dependency versions that are outdated relative to available versions |

## Target applicability

Not all checks apply to all 18 targets. Each check declares its applicability as one of three categories, which determines whether it runs for a given project based on the project's detected targets:

- **Universal** (`None`): runs for any target -- most project, release, and changelog checks
- **Workspace-only** (`"workspace"`): runs only in monorepo workspaces, target-agnostic
- **Target-specific** (`frozenset`): requires specific language targets with import scanners or AST analysis

:-: table-feature-matrix

### Excluded targets

Some checks explicitly exclude specific targets where the compiler or language toolchain already enforces the same constraint natively, making rlsbl's check redundant. These exclusions prevent false positives and unnecessary warnings:

| Check | Excluded target | Reason |
| --- | --- | --- |
| `circular-deps` | go | Go compiler rejects circular imports |

## Check metadata

Checks are declared in `rlsbl/data/checks.toml` with metadata that controls execution order, dependency resolution, and result severity. Each check entry has the following fields that the check runner uses to determine when and how to execute the check:

| Field | Type | Description |
| --- | --- | --- |
| `tags` | array of strings | Which tags include this check (empty = untagged, only runs with `--all` or `--name`) |
| `severity` | `"error"` or `"warn"` | Whether failure blocks (`fail`) or advises (`warn`) |
| `fast` | bool | Whether the check completes quickly (used for prioritization) |
| `pure` | bool | Whether the check starts only allowlisted read-only programs (see [Purity](#purity) below) |
| `needs_network` | bool | Whether the check requires network access (e.g., GitHub API calls) |
| `depends_on` | array of strings | Other checks that must pass first (skipped if dependency fails) |

Checks are implemented via the `@app.error_check("<name>")` and `@app.warn_check("<name>")` decorators in the `rlsbl/checks/` package (one module per tag, e.g. `project.py`, `release.py`, `workspace.py`), which register the function with strictcli's check system. The name passed to the decorator must match the key in `checks.toml`, and the decorator chosen must match that entry's `severity`.

### Purity

**A pure check starts only allowlisted read-only programs.** The allowlist is `rlsbl/observe_allowlist.py`, whose written standard is *no user-visible mutation*: ref updates, index writes and credential emission are refused there, so any program that reaches the list changes nothing a user would notice. A check that starts no program at all is trivially pure.

A check is impure when it starts a program that is not on that list. Every impure check today runs a tool that writes: `ruff` rewrites files, `uv sync` materializes an environment, the test suites and gradle build.

Purity decides what a preview does. Under `rlsbl release run --dry-run` the preflight runs its pure checks for real and lists the impure ones as `would run: <name> (impure)` -- so a preview reports real findings from everything that can be run without changing anything, and is honest about the rest.

This rule replaced an older one, "the check starts no program at all". That rule forced nine checks that spawn only read-only local git (the changelog validators, the two pre-push checks, `workspace-unregistered`, `go-companion-tags`) to be declared impure, and it quietly misdeclared two that do spawn: `local-tag` runs `git tag --list`, and `config-schema` can reach `go list` on its error path. All eleven are pure under the current rule, and are now declared so deliberately rather than by accident.

The declaration is verified, not trusted: `tests/test_check_purity.py` executes every pure-declared check under an effects observer and fails on any spawn whose argv matches no allowlist prefix.

`needs_network` is orthogonal: it says whether a check needs the network to answer at all, never whether it may mutate. A pure check may be a network read.

## Examples

### Running all checks before a release

```bash
rlsbl check --all
#   lock .......................... pass
#   version-consistency ........... pass
#   config-schema ................. pass
#   license-file .................. pass
#   scaffold-conflicts ............ pass
#   cross-repo-path-sources ....... pass
#   changelog-hashes .............. pass
#   changelog-range ............... pass
#   changelog-coverage ............ FAIL
#     Uncovered commits:
#       a1b2c3d  Add retry logic
#       e4f5g6h  Fix timeout bug
#   changelog-schema .............. pass
#   changelog-user-facing ......... warn  No user-facing entries
#   local-tag .................... warn  No tag for v0.5.3
#   test-suite ................... pass
#
#   12 passed, 1 failed, 2 warnings
```

### Investigating a specific check failure

```bash
# Run just the failing check to see detailed output
rlsbl check --name changelog-coverage
#   changelog-coverage ............ FAIL
#     Uncovered commits:
#       a1b2c3d  Add retry logic
#       e4f5g6h  Fix timeout bug
#     Fix: run `rlsbl changelog add --commits <hash> ...` for each

# Fix it
rlsbl changelog add --commits a1b2c3d --description "Add retry logic to HTTP client" --type feature
rlsbl changelog add --commits e4f5g6h --description "Fix timeout crash on slow connections" --type fix

# Verify the fix
rlsbl check --name changelog-coverage
#   changelog-coverage ............ pass
```

### Checking workspace integrity in a monorepo

```bash
rlsbl check --tag workspace
#   workspace-ci-router ........... pass
#   workspace-ci-synced ........... pass
#   workspace-targets ............. pass
#   workspace-unregistered ........ FAIL
#     packages/new-lib/ has pyproject.toml but is not in workspace.toml
#   workspace-stale-entries ....... pass
#   dev-only-boundary ............. pass
#   dead-workspace-packages ....... warn  library 'old-utils' not imported by any workspace package
#
#   Fix: run `rlsbl monorepo add --path packages/new-lib --target pypi`
```

### Pre-push check output

```bash
# Triggered automatically by git push, or run manually:
rlsbl check --tag prepush
#   prepush-changelog-coverage .... pass
#   prepush-gitignore-guard ....... pass
#   prepush-manual-warning ........ skip  (not a release branch push)
#   test-suite .................... pass
#   scaffold-conflicts ............ pass
```
