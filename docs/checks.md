---
description: "Complete reference for rlsbl's 44 project checks across 5 tags, including check metadata, severity levels, and target applicability."
---

# Check system

rlsbl includes 44 checks across 5 tags, run via the `rlsbl check` command. Checks validate project metadata, release state, changelog structure, workspace integrity, and code quality.

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

Each check returns one of four statuses:

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
| `workspace` | Monorepo workspace integrity and dependency rules | 8 |
| `quality` | Code quality, dependency analysis, scaffold hygiene | 7 |

Four checks (`layers-violations`, `deps-unused`, `deps-undeclared`, `deps-stale`) have no tag and only run with `--all` or `--name`.

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

## Untagged checks

These checks run only with `--all` or `--name`:

| Check | Severity | Description |
| --- | --- | --- |
| `layers-violations` | error | Dependency direction violates architectural layer rules defined in `workspace.toml` |
| `deps-unused` | error | Declared dependencies that are never imported |
| `deps-undeclared` | error | Imported packages that are not declared as dependencies |
| `deps-stale` | error | Workspace dependency versions that are outdated relative to available versions |

## Target applicability

Not all checks apply to all targets. Three applicability categories exist:

- **Universal** (`None`): runs for any target -- most project, release, and changelog checks
- **Workspace-only** (`"workspace"`): runs only in monorepo workspaces, target-agnostic
- **Target-specific** (`frozenset`): requires specific language targets with import scanners or AST analysis

:-: table-feature-matrix

### Excluded targets

Some checks explicitly exclude targets where the compiler or toolchain already enforces the same constraint:

| Check | Excluded target | Reason |
| --- | --- | --- |
| `circular-deps` | go | Go compiler rejects circular imports |

## Check metadata

Checks are declared in `rlsbl/data/checks.toml` with the following metadata fields:

| Field | Type | Description |
| --- | --- | --- |
| `tags` | array of strings | Which tags include this check (empty = untagged, only runs with `--all` or `--name`) |
| `severity` | `"error"` or `"warn"` | Whether failure blocks (`fail`) or advises (`warn`) |
| `fast` | bool | Whether the check completes quickly (used for prioritization) |
| `pure` | bool | Whether the check has no side effects and reads only local state |
| `needs_network` | bool | Whether the check requires network access (e.g., GitHub API calls) |
| `depends_on` | array of strings | Other checks that must pass first (skipped if dependency fails) |

Checks are implemented via the `@app.check()` decorator in `rlsbl/checks.py`, which registers the function with strictcli's check system. The decorator name must match the key in `checks.toml`.
