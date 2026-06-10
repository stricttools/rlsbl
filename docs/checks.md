---
description: "Complete reference for rlsbl's 50 checks across 6 tags, including check metadata, severity levels, and target applicability."
---

# Check system

:-: check-count

Run checks via the `rlsbl check` command. Checks are organized across 6 tags (project, release, changelog, workspace, quality, prepush) and validate project metadata, release state, changelog structure, workspace integrity, code quality, and pre-push enforcement. Four additional untagged checks run only with `--all` or `--name`.

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
| `project` | Project-level metadata, config schema, version consistency | 12 |
| `release` | Git tag and GitHub Release validation | 4 |
| `changelog` | JSONL changelog validation and structure | 9 |
| `workspace` | Monorepo workspace integrity and dependency rules | 9 |
| `quality` | Code quality, dependency analysis, scaffold hygiene | 8 |
| `prepush` | Pre-push enforcement: changelog coverage, gitignore guard, manual-push warning, tests | 5 |

Note: `test-suite` is tagged both `prepush` and `quality`, so it appears in both tag counts. Four checks (`layers-violations`, `deps-unused`, `deps-undeclared`, `deps-stale`) have no tag and only run with `--all` or `--name`.

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
| `private-publish-workflow` | error | Private repos must not have a publish workflow that pushes to public registries |
| `npm-private-mismatch` | error | `package.json` private field matches `.rlsbl/config.json` private flag (npm targets only) |
| `target-version-readable` | error | Version can be read from all declared target files |
| `selfdoc-version-drift` | error | selfdoc-generated version references match the actual project version |

## Release checks

| Check | Severity | Description |
| --- | --- | --- |
| `local-tag` | warn | A git tag exists locally for the current version |
| `remote-tag` | warn | The version tag has been pushed to the remote (requires network) |
| `github-release` | warn | A GitHub Release exists for the current version tag (requires network) |
| `branch-sync` | error | Local branch is not behind the remote tracking branch (requires network) |

Release checks form a dependency chain: `version-consistency` -> `local-tag` -> `remote-tag` -> `github-release`. If an upstream check fails, downstream checks are skipped.

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

Dependencies: `changelog-range` and `changelog-coverage` depend on `changelog-hashes` (hash resolution must succeed first).

## Workspace checks

| Check | Severity | Description |
| --- | --- | --- |
| `workspace-ci-router` | error | The monorepo CI router workflow exists and routes to all registered projects |
| `workspace-ci-synced` | error | Per-project workflows in `.github/workflows/` match their scaffolded sources |
| `workspace-targets` | error | Each project's declared target matches its actual manifest files |
| `workspace-unregistered` | error | No project directories with manifest files exist outside of `workspace.toml` |
| `workspace-stale-entries` | error | No `workspace.toml` entries point to directories that no longer exist |
| `dev-node-boundary` | error | No non-dev-node project has a runtime dependency on a dev_node project |
| `dead-workspace-packages` | warn | Detects workspace packages with no commits since their last release |
| `subtree-remote-reachable` | error | Configured subtree remote URLs are reachable (requires network) |

## Quality checks

| Check | Severity | Description |
| --- | --- | --- |
| `dead-modules` | warn | Detects source modules with no inbound imports (unreachable code) |
| `circular-deps` | warn | Detects circular import dependencies between modules |
| `library-lint` | error | Runs lint rules for library projects (API surface, exports) |
| `deps-runtime-test-only` | warn | Runtime dependencies that are only imported in test files |
| `deps-dev-in-lib` | error | Dev dependencies used in library source (should be runtime deps) |
| `scaffold-unreplaced-vars` | error | Leftover `{{...}}` template placeholders in workflow files |
| `scaffold-conflict-markers` | error | Unresolved merge conflict markers in scaffolded files |

## Prepush checks

| Check | Severity | Description |
| --- | --- | --- |
| `prepush-changelog-coverage` | error | Verifies every pushed commit has a JSONL changelog entry |
| `prepush-gitignore-guard` | error | Blocks push if rlsbl-managed files are gitignored |
| `prepush-manual-warning` | warn | Warns on manual push to release branch (non-blocking) |
| `test-suite` | error | Runs project tests (`pytest` / `go test` / `npm test`) |
| `test-suite-workspace` | error | Runs tests for affected workspace projects (monorepo only) |

Dependencies: `test-suite` and `test-suite-workspace` both depend on `prepush-changelog-coverage` -- fast checks fail first, so the test suite is skipped if changelog coverage fails. `test-suite` is also tagged `quality`, so it runs under both `rlsbl check --tag prepush` and `rlsbl check --tag quality`.

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
| `pure` | bool | Whether the check has no side effects and reads only local state |
| `needs_network` | bool | Whether the check requires network access (e.g., GitHub API calls) |
| `depends_on` | array of strings | Other checks that must pass first (skipped if dependency fails) |

Checks are implemented via the `@app.check()` decorator in `rlsbl/checks.py`, which registers the function with strictcli's check system. The decorator name must match the key in `checks.toml`.
