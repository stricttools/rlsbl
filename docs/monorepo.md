---
description: "Guide to rlsbl monorepo workspaces: workspace.toml, dependency graph renderings, batch releases, impact analysis, snapshots, mirrors, CI router."
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

Lint config resolves at two levels: a member's own `.rlsbl/lint/<language>.toml` wins when present, otherwise a releasable member falls back to the shared `.rlsbl-monorepo/releasables/<name>/lint/<language>.toml`. `rlsbl monorepo cleanup` removes a member's `.rlsbl/lint/` only when it is byte-identical to that shared config (a genuine override is preserved).

### Dev nodes (`dev_node = true`)

Dev nodes are projects at the edge of the dependency graph that nothing user-facing depends on — test infrastructure, conformance suites, dev tooling, and internal utilities consumed only during development. Dev nodes cannot be released:

- **No changelog system**: no `.rlsbl/changes/`, no `unreleased.jsonl`, no `CHANGELOG.md`
- **No releases**: `rlsbl release run` and `rlsbl release edit` error with "dev_node projects cannot be released"
- `rlsbl changelog add` errors with "dev node projects don't use changelogs"
- Scaffold skips changelog infrastructure
- Pre-push check ignores dev node commits
- Batch release (`rlsbl monorepo release run`) excludes dev nodes
- Remove `dev_node = true` from workspace.toml to make a project releasable
- The `dev-only-boundary` check prevents non-dev-node projects from declaring runtime dependencies on dev nodes

## Dependency graph

The workspace builds a directed dependency graph from two complementary sources, combining automatic manifest scanning with explicit declarations to capture all inter-project relationships. This graph drives topological release ordering, impact analysis, dead-package detection, and the dev-only boundary guardrail that prevents user-facing projects from depending on dev nodes:

1. **Manifest scanning** — pluggable scanners (`PypiScanner`, `NpmScanner`, `DartScanner`) parse each project's manifest file looking for intra-workspace dependencies
2. **Explicit `depends_on`** — the workspace.toml field adds edges the scanners cannot detect

Dependencies have a `scope` attribute with 4 possible values: `runtime`, `dev`, `peer`, or `explicit`. The scope determines which edges the `dev-only-boundary` check considers (only 2 of the 4 scopes -- `runtime` and `explicit` -- trigger the boundary violation).

### Viewing the graph

The graph has two renderings selected by `--format` -- DOT for Graphviz and an indented text tree for terminal inspection -- plus the machine form under the framework-owned `--json`, which puts the structured graph in the envelope's `payload` (see [Machine output](utilities.md#machine-output)). Every form supports the same filtering options, including scoping to a single root package and its transitive dependencies, reverse dependency queries showing what depends on a given package, and depth limiting to control how many levels of the graph are traversed:

```bash
# Text tree, indented (default)
rlsbl monorepo graph

# DOT format for Graphviz
rlsbl monorepo graph --format dot --output graph.dot

# Structured graph in the envelope's payload
rlsbl monorepo graph --json

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
rlsbl monorepo release order
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

The command reports a structured breakdown of the blast radius across 5 output sections, organized by dependency distance from the changed package. Each section helps answer a different question about what to test, review, and release:

| Section | Meaning |
| ------- | ------- |
| Input packages | The directly changed packages |
| Direct dependents | Packages with an immediate edge to the changed package |
| Transitive dependents | All packages reachable via BFS on reverse deps |
| Test scope | Packages that should be tested (input + all dependents) |
| Release candidates | Packages that may need a new release |

Supports `--depth N` to limit BFS traversal depth (default: unlimited, traverses the full transitive closure). The same breakdown is available as a structured document under the framework-owned `--json`, in the envelope's `payload` (see [Machine output](utilities.md#machine-output)).

## Batch release

`rlsbl monorepo release run` releases multiple packages in a single coordinated flow, respecting topological order so that leaf packages (those with no intra-workspace dependencies) are released first, followed by their dependents. This ensures downstream packages always reference the latest versions of their workspace dependencies.

### Workflow

1. Run `rlsbl monorepo release init` to scaffold `.rlsbl-monorepo/releases/unreleased.toml`
2. Edit the file: set bump type, description, and context per package
3. Run `rlsbl monorepo release run --watch --approve-consequential`

### release init scaffolding

`rlsbl monorepo release init` auto-detects release targets for each workspace project and generates a TOML file with pre-populated per-package sections. Packages with no unreleased commits are commented out, and dev nodes are excluded entirely since they cannot be released:

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
rlsbl monorepo snapshot-check
```

The snapshot contains:

- All package names, paths, versions, and targets
- Dependency edges with type, constraint, and scope
- Graph metadata (topological order, leaf nodes, root nodes)
- Timestamp of generation

The snapshot is auto-committed with an `Autogenerated: true` trailer (exempt from changelog coverage). Use `rlsbl monorepo snapshot-check` in CI to ensure the snapshot stays current: it is read-only and exits 1 when the artifact is stale or missing.

## Mirror

`rlsbl monorepo mirror <project>` reconciles a workspace project's subtree mirror — a standalone git repository containing only that project's subtree history, plus its own rlsbl scaffold and CI workflows for independent publishing. Consumers can clone just the one project without the full monorepo.

### The mirror is tool-owned

The mirror is a **derived artifact**: it is regenerated from the monorepo and **nothing is ever authored on it by hand**. Because the mirror is fully derived, force-push (with lease) is the *routine* write, not an exceptional one — every convergence rewrites `main` to match the monorepo's current state.

Treat mirror repositories as read-only downstreams. To change a project, change it in the monorepo and re-run `mirror`. Never commit to a mirror directly: a hand-authored commit is a contract violation that the reconciler refuses to erase silently (see the tripwire below).

### Requirements

- The project must have `subtree_remote` configured in workspace.toml
- SSH host must be consistent between `subtree_remote` and origin
- Recommended: enable **branch protection** on the mirror's `main` for humans while allowing the automation identity to force-push, so the tool-owned contract is enforced at the remote too

### Plan and apply

The mirror command follows an observe-then-converge reconciliation pattern. In dry-run mode it inspects the current state of the remote mirror and the local monorepo, produces a human-readable plan describing what would change, and exits without writing anything. In apply mode it executes the convergence steps, force-pushing with lease to update the mirror to match the current subtree state:

- `rlsbl monorepo mirror <project> --dry-run` — observe and print a plan; makes **zero writes** (beyond the loose objects a branchless subtree split leaves in the monorepo's own object store).
- `rlsbl monorepo mirror <project>` — observe, then converge (apply).

The desired state of the mirror's `main` is exactly one scaffold commit atop the **current split-lineage commit**, where that commit is the deterministic branchless `git subtree split` of the project's current history, and the scaffold commit touches only scaffold-owned paths.

Observation reports one of:

| State | Meaning | What apply does |
| --- | --- | --- |
| `converged` | Scaffold commit atop the current split. | Nothing — clean no-op. |
| `behind` | A scaffold layer atop an **older** split; a new split is available (shows old → new). | Force-push the new split (with lease), then re-scaffold. |
| `scaffold-missing` | The tip is a bare split commit with no scaffold layer (the pre-scaffold-layer shape). May also be behind. | Add the scaffold commit (and push a new split first if behind). |
| `contract-violated` | A foreign, hand-authored commit exists on the mirror. | **Hard error, touches nothing.** Lists the offending commit(s) and paths, and tells you to either port the change into the monorepo or reset the mirror branch, then re-run. |
| `remote-missing-or-empty` | Virgin remote. | Push the split, then scaffold CI. |

Apply is **idempotent**: re-running on a converged mirror is a clean no-op, and an interrupted apply (killed between the split push and the scaffold commit) heals on the next run — it re-observes as `scaffold-missing` and adds the scaffold layer.

### The tripwire

Convergence never blindly overwrites the mirror. The remote tip must be **either** a bare split-lineage commit (the current split SHA or an older one — this covers legacy mirrors that never received a scaffold layer) **or** exactly one commit atop a split-lineage commit whose changed paths are all scaffold-owned (`.rlsbl/`, `.github/`, and a small set of root files like `CHANGELOG.md`). Anything else is a foreign commit: apply refuses and reports it. This makes contract violations *loud* instead of silently force-erased.

> Note: `rlsbl monorepo sync` does **not** update mirror repositories. `sync` regenerates the monorepo's own `.github/workflows`. Mirrors are updated only by re-running `rlsbl monorepo mirror <project>` (for example after a release).

## Sync

`rlsbl monorepo sync` folds every project's CI jobs into a single generated router at the repository root's shared `.github/workflows/` directory, performing template variable resolution along the way. This is required because GitHub Actions only reads workflows from the repository root, not from individual project subdirectories.

The sync process:

1. For each project in the workspace, reads its scaffolded CI workflow
2. Injects `working-directory` into job steps so they run in the correct subdirectory
3. Inlines every project's jobs into one generated `ci-router.yml`, keyed by a per-file prefix and gated on a `detect` job's paths filter, and inlines publish jobs into `publish.yml` the same way
4. Removes any stale per-project workflow copy left at the root by an older sync (via saferm)
5. Commits the generated routers

Jobs are inlined rather than invoked as reusable workflows: GitHub rejects a workflow file that references 20 or more of them, so `uses:`-based routing cannot scale past a certain workspace size. A guardrail refuses to emit a generated router containing any reusable call at all. Each inlined job gets an explicit `name: "{prefix} / {job}"`, so check-run names are identical to the ones the reusable-workflow era produced and the publish gate's regexes and any branch protection rules keep matching.

This ensures every project has its CI pipeline properly wired even when using different targets or custom workflow steps.

### Router paths filters

The generated router gates each project's inlined jobs on a `dorny/paths-filter` entry built from that project's `path` plus its `watch` patterns. A push whose diff matches none of a project's patterns leaves that project's CI job `skipped` on the pushed commit.

In explicit releasable mode, one more pattern is appended to **every** member of a releasable: the releasable's own `CHANGELOG.md` under `.rlsbl-monorepo/releasables/<name>/`. It is a single path shared by all members, so any commit that touches it matches all of their filters at once. This is a deliberate run-everything hook. A release commit may touch nothing under a member's own directory -- guaranteed on a first release, where the version write is a no-op -- and the publish gate refuses to treat that member's `skipped` check as passing, with no re-runnable recovery. Since the release commit always regenerates and commits the releasable `CHANGELOG.md`, anchoring every member's filter on it makes the release commit verifiable for all members.

Be aware of the cost: **releasing a releasable runs the CI jobs of every one of its members**, including members whose own code did not change. That is the accepted trade, not a bug -- see [Publish gating](release-workflow.md#the-releasable-run-everything-hook) in the release workflow docs for the full rationale, including why the gate is never relaxed to accept `skipped`, and what a push that touches only non-member paths (a dev node's directory, for instance) looks like.

### Running every job on one commit (`run_all`)

The router declares a `workflow_dispatch` input, `run_all`. Dispatching with `run_all=true` short-circuits the paths filter: every inlined job's condition is `(needs.detect.outputs.<project> == 'true' || inputs.run_all)`, so all of them run on the dispatched commit.

```bash
gh workflow run ci-router.yml --ref main -f run_all=true
```

This is the sanctioned exit from a candidate whose push window is honestly narrow. A first release candidate rides the run-everything hook and runs every member's CI; if some of those jobs fail, the fix-forward commits that heal them touch only the members they fix. The *next* candidate's window therefore covers only those members, every other member's job concludes `skipped`, and the release gate refuses -- correctly, because a skipped check proves nothing about the commit. Widening the window would mean committing churn under paths that did not change, which lies in both the history and the changelog. Dispatching `run_all` re-runs the **same** commit with the filter short-circuited instead.

Nothing is waived by the dispatch. The jobs execute for real, and a job that fails there still blocks the release. Both gates collapse matching check runs to the latest per name, so the dispatched run's conclusions supersede the earlier `skipped` ones on that commit -- and a red conclusion supersedes just as readily as a green one. The router's concurrency group includes the input, so a `run_all` dispatch never cancels an in-flight push run for the same commit (a cancelled run is a red verdict at the workflow-run level, before any per-check collapse happens).

One wrinkle both gates handle explicitly: GitHub does not expand a matrix for a job its `if` skipped. The skipped job collapses to a single check run under the unsuffixed name (`cli-ci / test`), while the run that executes it emits one per leg (`cli-ci / test (3.12)`). They never share a name, so a plain per-name collapse would leave the skip standing. A `skipped` check is therefore dropped when a strictly later check run for the **same job** -- its matrix expansion, matched by name -- exists; the legs are then judged on their own conclusions. Nothing else can cover a skip: not a sibling job, not a merely prefix-sharing name (`test-extra` is a different job), and not an earlier run. If the skip is the latest word about that job, it stands and the gate refuses.

Typical sequence when a release stops at a skipped member:

```bash
gh workflow run ci-router.yml --ref main -f run_all=true
gh run watch <run-id>
rlsbl release resume
```

## Workspace checks

Fourteen checks run under `rlsbl check --tag workspace`, covering CI configuration consistency, project registration hygiene, dependency boundary enforcement, buildability, and code liveness. All error-severity checks block releases when they fail:

| Check | Severity | Description |
| ----- | -------- | ----------- |
| `workspace-ci-router` | error | Verifies the generated `ci-router.yml` exists at the repo root (it holds every project's inlined jobs; per-project coverage is `workspace-ci-synced`) |
| `workspace-ci-synced` | error | Verifies each in-scope project's CI jobs are inlined into the shared `ci-router.yml` |
| `workspace-targets` | error | Every project must have at least one detectable release target |
| `workspace-unregistered` | error | Detects project directories with manifests that are not in workspace.toml |
| `workspace-stale-entries` | error | Detects workspace.toml entries pointing to non-existent directories |
| `dev-only-boundary` | error | Non-dev-only projects cannot have runtime dependencies on dev-only projects |
| `unversioned-boundary` | error | Releasable projects cannot have runtime dependencies on unversioned (`releasable = false`) projects |
| `dead-workspace-packages` | warn | Library projects with zero dependents (may indicate unused code) |
| `subtree-remote-reachable` | error | All configured subtree_remote URLs must be accessible (network check) |
| `workspace-unbuildable` | error | Workspace members build under `uv sync --all-packages` (pypi workspaces only) |
| `scaffold-gitignore-stale` | warn | Workspace project `.gitignore` files contain all rlsbl-managed entries |
| `root-rlsbl-conflict` | error | Root `.rlsbl/` must not coexist with `.rlsbl-monorepo/` |
| `go-companion-tags` | warn | Non-private Go members of releasables have companion tags for the current version |
| `test-suite-workspace` | error | Runs tests for affected workspace projects (also tagged `prepush`) |

Run all workspace checks:

```bash
rlsbl check --tag workspace
```

See [checks.md](checks.md) for the full check reference across all tags.

## Dev node boundary

The `dev-only-boundary` check is a structural guardrail that prevents misuse of the `dev_node` flag by ensuring dev-only projects remain true leaf nodes in the dependency graph, consumed by nothing user-facing. The rule:

> If a non-dev-node project has a **runtime dependency** on a dev_node project, `rlsbl check --tag workspace` errors.

This ensures dev nodes are truly leaf nodes consumed by nothing user-facing. The check distinguishes:

- **Runtime dependencies** (scope: `runtime` or `explicit`) — carry changes to users. These trigger the boundary violation.
- **Dev dependencies** (scope: `dev`) — only affect test/build environments. These are allowed.

If the boundary check fails, either:
1. Remove the `dev_node` flag from the dependency (it is not actually a dev-only project)
2. Move the runtime dependency to a dev dependency in the consumer's manifest

## Examples

### Setting up a monorepo from scratch

```bash
cd ~/Projects/my-monorepo
git init

# Initialize the workspace
rlsbl monorepo init
#   Created .rlsbl-monorepo/workspace.toml

# Add a Python library
mkdir -p packages/core
# ... create packages/core/pyproject.toml ...
rlsbl monorepo add --name core --path packages/core --target pypi --library

# Add an npm CLI that depends on the library
mkdir -p packages/cli
# ... create packages/cli/package.json ...
rlsbl monorepo add --name cli --path packages/cli --target npm --depends-on core

# Add a test suite (dev node -- no changelog, no releases)
mkdir -p packages/tests
rlsbl monorepo add --name tests --path packages/tests --dev-node

# Scaffold CI for each project
cd packages/core && rlsbl scaffold && cd ../..
cd packages/cli && rlsbl scaffold && cd ../..

# Sync all CI workflows to the repo root
rlsbl monorepo sync
#   Synced packages/core CI -> .github/workflows/ci-router.yml
#   Synced packages/cli CI -> .github/workflows/ci-router.yml
```

### Releasing multiple packages

```bash
# Check workspace status
rlsbl monorepo status
#   core   0.1.0  2 commits ahead of core@v0.1.0
#   cli    0.2.0  3 commits ahead of cli@v0.2.0
#   tests  (dev node -- not releasable)

# Scaffold the release file
rlsbl monorepo release init
#   Created .rlsbl-monorepo/releases/unreleased.toml

# Edit the release file:
#   [packages.core]
#   bump = "minor"
#   description = "Add async support to core API"
#
#   [packages.cli]
#   bump = "patch"
#   description = "Update CLI to use new async core API"

# Release in dependency order (core first, then cli)
rlsbl monorepo release run --no-allow-dirty --watch --approve-consequential
#   Release order: core, cli
#   Releasing core 0.1.0 -> 0.2.0 ...
#     Validating ... OK
#     Tests ... OK
#     Committing core@v0.2.0 ... OK
#   Releasing cli 0.2.0 -> 0.2.1 ...
#     Validating ... OK
#     Tests ... OK
#     Committing cli@v0.2.1 ... OK
#   Watching CI ...
```

### Analyzing the impact of a change

```bash
# What breaks if we change the core library?
rlsbl monorepo impact core
#   Input packages:     core
#   Direct dependents:  cli
#   Test scope:         core, cli
#   Release candidates: core, cli

# What changed since the last release?
rlsbl monorepo impact --since core@v0.1.0
#   Changed packages:   core
#   Direct dependents:  cli
#   Test scope:         core, cli
```

### Viewing the dependency graph

```bash
# Text tree format
rlsbl monorepo graph --format text
#   core
#     <- cli
#   tests (dev node)

# DOT format for visualization
rlsbl monorepo graph --format dot --output workspace.dot
dot -Tpng workspace.dot -o workspace.png
```

## Workspace module

The workspace module handles discovery, loading, saving, and resolution of monorepo workspaces. It walks the directory tree upward to locate the nearest `workspace.toml`, parses the TOML structure into validated `WorkspaceProject` entries, and writes changes back atomically using tomlkit to preserve formatting and comments.

:-: ref path="rlsbl.workspace"
