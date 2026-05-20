# Impact analysis: what breaks when a package changes

## Context

In a large monorepo, changing a foundational package cascades through many dependents. Before making a change, you want to know the blast radius. rlsbl already has the dependency graph and can compute reverse dependencies, but there's no user-facing command that answers "what is affected?"

## Decisions

- **Both CI optimization and developer tool**, equally important.
- **All three input formats**: package name, file paths, git diff.
- **File-level + package-level analysis** from the start. The import scanner (from dependency-import-validation) provides the file-level import graph.
- **Downstream flagging**: both proactive (impact analysis shows blast radius) and reactive (stale-internal-deps check catches it after the fact).

## Command

`monorepo impact <input> [--format=json|text] [--depth N]`

### Input formats

1. **Package name**: `monorepo impact models` -- shows all direct and transitive dependents.
2. **File paths**: `monorepo impact models/lib/src/money.dart` -- uses the import graph to determine which packages import symbols from this file, then expands transitively. More precise than package-level.
3. **Git diff**: `monorepo impact --since HEAD~3` or `monorepo impact --since v1.2.0` -- auto-detects affected packages from changed files in the git range.

### Output

For each affected package:
- **Direct dependents**: packages that directly depend on the changed package.
- **Transitive dependents**: all packages reachable via reverse dependency edges.
- **Test scope**: which packages' test suites should run to validate the change.
- **Release candidates**: which packages may need version bumps if the change is breaking.

JSON output for CI integration (selective test running). Text output for developer use.

### File-level granularity

The import scanner (from dependency-import-validation) builds a file-level import graph: for each source file, which packages does it import from? Inverting this gives: for each file in package X, which files in other packages import from it?

When the input is a file path:
1. Determine which package the file belongs to.
2. Find all files across the workspace that import from this file's package.
3. Optionally narrow: if the file is `models/lib/src/money.dart`, only flag packages that actually import `money.dart` (not all of `models/`).
4. Expand transitively.

This requires the import scanner to track import paths (not just package names). E.g., `import 'package:models/src/money.dart'` tells us this file specifically depends on `money.dart` in `models`.

## CI optimization use case

A CI job receives a list of changed files (from the PR diff). It runs `monorepo impact --since origin/main --format=json` to get the list of affected packages. Then it runs tests only for those packages.

This requires:
- Machine-readable JSON output with package names and test commands
- Integration with the CI router (`monorepo sync` generates per-package CI workflows)
- A way to override the selective scope (`--all` to force testing everything)

## Implementation

- `rlsbl/workspace_graph.py` -- add `transitive_rdeps(name)` method (BFS over `dependents()`)
- `rlsbl/commands/monorepo.py` -- new `_cmd_impact` subcommand
- Integration with import scanner for file-level analysis (once dependency-import-validation is implemented)
- Git diff parsing: `git diff --name-only <range>` -> map files to packages -> compute impact

## Prerequisites

- Dependency graph traversal (transitive rdeps) -- also needed by graph export
- Dependency-import-validation (for file-level granularity)
- Cross-language workspace support (for the import scanner)

## Effort

Package-level: small (transitive rdeps + git diff parsing). File-level: medium-large (depends on import scanner infrastructure).
