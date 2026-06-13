---
description: "Guide to rlsbl monorepo workspaces — workspace.toml format, dependency graphs, batch releases, impact analysis, snapshots, mirrors, and checks."
---

# Monorepo guide

rlsbl supports monorepo workflows via the `rlsbl monorepo` command family. A monorepo workspace manages multiple independently-versioned projects sharing one git repository, coordinated through a single `.rlsbl-monorepo/workspace.toml` file at the repository root. A single workspace can contain any mix of the 18 supported release targets.

## Getting started

```bash
# Initialize a monorepo workspace (creates .rlsbl-monorepo/ with workspace.toml)
rlsbl monorepo init

# Add projects to the workspace
rlsbl monorepo add --name mylib --path packages/mylib --target pypi --library
rlsbl monorepo add --name cli --path packages/cli --target npm
rlsbl monorepo add --name tests --path packages/tests --dev-node

# Scaffold CI for all projects
rlsbl scaffold

# Sync per-project CI workflows to shared .github/workflows/
rlsbl monorepo sync

# Show workspace status (versions, unreleased commits)
rlsbl monorepo status

# List all projects
rlsbl monorepo list
```

## workspace.toml format

The workspace file lives at `.rlsbl-monorepo/workspace.toml` and serves as the single source of truth for all project registrations, dependency declarations, and architectural layer rules. It uses TOML array-of-tables syntax for project declarations, with one `[[projects]]` block per sub-project:

```toml
[[projects]]
path = "packages/mylib"
name = "mylib"
target = "pypi"
library = true
watch = ["packages/mylib/**", "shared/types/**"]
depends_on = []

[[projects]]
path = "packages/cli"
name = "cli"
target = "npm"
depends_on = ["mylib"]
registry_name = "@org/cli"

[[projects]]
path = "packages/tests"
name = "tests"
dev_node = true

[layers]
order = ["foundation", "app"]

[layers.assignments]
foundation = ["mylib"]
app = ["cli"]

[layers.overrides]
unrestricted = ["tests"]
```

### Project fields

Each `[[projects]]` block supports 10 fields (1 required, 9 optional) that define the project's identity, release target, change detection scope, inter-project relationships, and behavioral flags. The only required field is `path` -- all other fields either have sensible defaults (like deriving `name` from the path basename) or are opt-in features that activate additional checks and behaviors:

| Field | Required | Type | Description |
| ----- | -------- | ---- | ----------- |
| `path` | yes | string | Relative path from repo root to the project directory |
| `name` | no | string | Project name (defaults to basename of `path`) |
| `target` | no | string | Release target (auto-detected if omitted) |
| `watch` | no | list of strings | Glob patterns for change detection beyond the project path |
| `subtree_remote` | no | string | Git remote URL for subtree mirror publishing |
| `depends_on` | no | list of strings | Explicit intra-workspace dependencies (project names) |
| `library` | no | bool | Mark as a shared library (enables library-lint check) |
| `dev_node` | no | bool | Mark as a dev-only project (no changelog, no CHANGELOG.md) |
| `registry_name` | no | string | Override package name on the registry (e.g., scoped npm name) |
| `description` | no | string | Short project description for documentation |

### Layers section

The optional `[layers]` section enforces architectural dependency direction by grouping projects into ordered layers and blocking imports that violate the hierarchy. Higher layers may depend on lower layers, but not vice versa. See [layers.md](layers.md) for full configuration reference.

## Project types

### Regular projects

Standard projects get the full release experience, including changelog enforcement, CI pipeline generation, and all workspace validation checks. This is the default project type when neither `library` nor `dev_node` flags are set:

- JSONL changelog with commit coverage enforcement
- Generated CHANGELOG.md
- CI workflows (test + publish)
- Pre-push hook enforcement
- All workspace checks apply

### Library projects (`library = true`)

Libraries are packages consumed by other workspace projects as runtime or dev dependencies. They get everything regular projects have, plus additional quality checks that ensure shared code stays clean and is actually used within the workspace:

- `library-lint` quality check (runs language-specific lint rules)
- `dead-workspace-packages` detection (warns if the library has no dependents)
- Built-in lint runs during `rlsbl release run` (non-libraries skip built-in lint)

### Dev nodes (`dev_node = true`)

Dev nodes are projects at the edge of the dependency graph that nothing user-facing depends on — test infrastructure, conformance suites, dev tooling, and internal utilities consumed only during development. They skip the entire changelog system and use a streamlined release flow with a mandatory description that becomes the GitHub Release body. They have a streamlined release flow:

- **No changelog system**: no `.rlsbl/changes/`, no `unreleased.jsonl`, no `CHANGELOG.md`
- `rlsbl changelog add` errors with "dev node projects don't use changelogs"
- Scaffold skips changelog infrastructure
- Pre-push check ignores dev node commits
- Release flow: version bump, commit, tag, push, GitHub Release (skipping changelog validation/generation)
- Release description in `unreleased.toml` is **mandatory** for dev nodes (it becomes the GitHub Release body)
- The `dev-node-boundary` check prevents non-dev-node projects from declaring runtime dependencies on dev nodes

## Dependency graph

The workspace builds a directed dependency graph from two complementary sources, combining automatic manifest scanning with explicit declarations to capture all inter-project relationships. This graph drives topological release ordering, impact analysis, dead-package detection, and the dev-node boundary guardrail that prevents user-facing projects from depending on dev nodes:

1. **Manifest scanning** — pluggable scanners (`PypiScanner`, `NpmScanner`, `DartScanner`) parse each project's manifest file looking for intra-workspace dependencies
2. **Explicit `depends_on`** — the workspace.toml field adds edges the scanners cannot detect

Dependencies have a `scope` attribute with 4 possible values: `runtime`, `dev`, `peer`, or `explicit`. The scope determines which edges the `dev-node-boundary` check considers (only 2 of the 4 scopes -- `runtime` and `explicit` -- trigger the boundary violation).

### Viewing the graph

```bash
# JSON format (default)
rlsbl monorepo graph

# DOT format for Graphviz
rlsbl monorepo graph --format dot --output graph.dot

# Text tree (indented)
rlsbl monorepo graph --format text

# Filter to a single package's transitive dependencies
rlsbl monorepo graph --root mylib

# Filter to reverse dependencies (what depends on mylib)
rlsbl monorepo graph --reverse mylib

# Limit depth
rlsbl monorepo graph --root mylib --depth 2
```

### Topological order

```bash
# Show release order (leaves first, dependents after their dependencies)
rlsbl monorepo release-order
```

Uses Kahn's algorithm. Projects with no dependencies appear first. Detects and reports circular dependencies as a hard error.

## Impact analysis

`rlsbl monorepo impact` computes the blast radius of a change by performing BFS on the reverse dependency graph, showing every direct and transitive dependent that could be affected. This helps determine which packages need testing and which are release candidates after a change.

### Three input modes

```bash
# By package name
rlsbl monorepo impact mylib

# By file path (maps to containing package)
rlsbl monorepo impact packages/mylib/src/core.py

# By git diff range (all changed files since a ref)
rlsbl monorepo impact --since v0.5.0
```

### Output (impact)

The command reports a structured breakdown of the blast radius, organized by dependency distance from the changed package. Each section helps answer a different question about what to test, review, and release:

| Section | Meaning |
| ------- | ------- |
| Input packages | The directly changed packages |
| Direct dependents | Packages with an immediate edge to the changed package |
| Transitive dependents | All packages reachable via BFS on reverse deps |
| Test scope | Packages that should be tested (input + all dependents) |
| Release candidates | Packages that may need a new release |

Supports `--depth N` to limit BFS traversal depth (default: unlimited, traverses the full transitive closure).

## Batch release

`rlsbl monorepo release` releases multiple packages in a single coordinated flow, respecting topological order so that leaf packages (those with no intra-workspace dependencies) are released first, followed by their dependents. This ensures downstream packages always reference the latest versions of their workspace dependencies.

### Workflow

1. Run `rlsbl monorepo release-init` to scaffold `.rlsbl-monorepo/releases/unreleased.toml`
2. Edit the file: set bump type, description, and context per package
3. Run `rlsbl monorepo release --watch --yes`

### release-init scaffolding

`rlsbl monorepo release-init` auto-detects release targets for each workspace project and generates a TOML file with pre-populated per-package sections. Packages with no unreleased commits are commented out, and dev nodes are excluded entirely since they bypass the changelog system:

```toml
[packages.mylib]
bump = "patch"
description = ""
include = ["pypi"]

[packages.cli]
bump = "minor"
description = ""
include = ["npm"]

# [packages.tests]
# No unreleased commits since tests@v0.3.0
```

- Dev nodes are excluded entirely (they have no changelog)
- Packages with zero unreleased commits since their last tag are rendered as commented-out sections
- Each section's `include` list is pre-populated from detected targets

### Execution

Each package is released sequentially using the standard single-package release flow (validation, tests, version bump, commit, tag, push, GitHub Release). The batch orchestrator determines execution order from the workspace dependency graph:

1. Validate all listed packages exist in workspace
2. Build topological order from the full workspace graph
3. Filter to only packages in the batch, preserving topological order
4. Release each package in order

### Partial failure

If a package's release fails mid-batch, there is no automatic resume. The command prints what succeeded, then re-raises the error. To recover, fix the issue, remove already-released packages from the batch file, and re-run.

## Snapshot

`rlsbl monorepo snapshot` generates a committed JSON artifact at `.rlsbl-monorepo/snapshot.json` that captures the entire workspace state, including package metadata, dependency edges, and the computed topological order. This artifact is useful for CI verification and external tooling that needs to inspect workspace structure without parsing TOML.

```bash
# Generate and commit snapshot
rlsbl monorepo snapshot

# Verify snapshot is up-to-date (exits 1 if stale)
rlsbl monorepo snapshot --check
```

The snapshot contains:

- All package names, paths, versions, and targets
- Dependency edges with type, constraint, and scope
- Graph metadata (topological order, leaf nodes, root nodes)
- Timestamp of generation

The snapshot is auto-committed with an `Autogenerated: true` trailer (exempt from changelog coverage). Use `--check` in CI to ensure the snapshot stays current.

## Mirror

`rlsbl monorepo mirror <project>` initializes a subtree mirror repository for a workspace project, enabling consumers to clone just one project without the full monorepo. The mirror is a standalone git repository containing only the project's subtree history, with its own rlsbl scaffold and CI workflows for independent publishing.

### Requirements

- The project must have `subtree_remote` configured in workspace.toml
- The remote must be reachable (validated via `git ls-remote`)
- SSH host must be consistent between subtree_remote and origin

### Steps performed

1. Validates remote reachability and SSH host consistency
2. Runs `git subtree split --prefix=<path>` to extract the project's history
3. Pushes the split branch to the configured `subtree_remote`
4. Clones the mirror to a temp directory
5. Scaffolds rlsbl CI in the mirror
6. Pushes the scaffolded result

After initial setup, `rlsbl monorepo sync` keeps mirror repositories updated after releases.

## Sync

`rlsbl monorepo sync` copies per-project CI workflows to the shared `.github/workflows/` directory at the repository root, performing template variable resolution and trigger rewriting along the way. This is required because GitHub Actions only reads workflows from the repository root, not from individual project subdirectories.

The sync process:

1. For each project in the workspace, reads its scaffolded CI workflow
2. Rewrites the `on:` trigger to `workflow_call:` (making it callable from a router)
3. Injects `working-directory` into job steps so they run in the correct subdirectory
4. Generates a router workflow that dispatches to per-project workflows based on changed paths
5. Commits the synced workflows

This ensures every project has its CI pipeline properly wired even when using different targets or custom workflow steps.

## Workspace checks

Eight checks run under `rlsbl check --tag workspace` (7 error-severity, 1 warning-severity), covering CI configuration consistency, project registration hygiene, dependency boundary enforcement, and code liveness. All error-severity checks block releases when they fail:

| Check | Severity | Description |
| ----- | -------- | ----------- |
| `workspace-ci-router` | error | Validates the CI router workflow dispatches to all registered projects |
| `workspace-ci-synced` | error | Verifies per-project workflows in `.github/workflows/` match their scaffolded source |
| `workspace-targets` | error | Every project must have at least one detectable release target |
| `workspace-unregistered` | error | Detects project directories with manifests that are not in workspace.toml |
| `workspace-stale-entries` | error | Detects workspace.toml entries pointing to non-existent directories |
| `dev-node-boundary` | error | Non-dev-node projects cannot have runtime dependencies on dev_node projects |
| `dead-workspace-packages` | warn | Library projects with zero dependents (may indicate unused code) |
| `subtree-remote-reachable` | error | All configured subtree_remote URLs must be accessible (network check) |

Run all workspace checks:

```bash
rlsbl check --tag workspace
```

See [checks.md](checks.md) for the full check reference across all tags.

## Dev node boundary

The `dev-node-boundary` check is a structural guardrail that prevents misuse of the `dev_node` flag by ensuring dev-only projects remain true leaf nodes in the dependency graph, consumed by nothing user-facing. The rule:

> If a non-dev-node project has a **runtime dependency** on a dev_node project, `rlsbl check --tag workspace` errors.

This ensures dev nodes are truly leaf nodes consumed by nothing user-facing. The check distinguishes:

- **Runtime dependencies** (scope: `runtime` or `explicit`) — carry changes to users. These trigger the boundary violation.
- **Dev dependencies** (scope: `dev`) — only affect test/build environments. These are allowed.

If the boundary check fails, either:
1. Remove the `dev_node` flag from the dependency (it is not actually a dev-only project)
2. Move the runtime dependency to a dev dependency in the consumer's manifest

## Workspace module

The workspace module handles discovery, loading, saving, and resolution of monorepo workspaces. It walks the directory tree upward to locate the nearest `workspace.toml`, parses the TOML structure into validated `WorkspaceProject` entries, and writes changes back atomically using tomlkit to preserve formatting and comments.

:-: ref path="rlsbl.workspace"
