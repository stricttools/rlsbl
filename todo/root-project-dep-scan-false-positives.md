# Root-level project dep scan false positives

## Problem

When a monorepo workspace project has `path = "."` in `workspace.toml`, the dependency scanner's `walk_source_files()` walks the entire repository root. This picks up source files from sibling projects, producing false positive `deps-undeclared` findings.

## Root cause

`walk_source_files()` does not exclude directories that belong to other workspace projects. For a project at `path = "."`, the walk starts at the repo root and descends into every subdirectory, including those registered as separate projects in `workspace.toml`.

## Affected repos

- **gamehome**: dijkstra has `path = "."` in `workspace.toml`. The scan walks the entire repo root, finding imports from framework, mcp, and server source files. This produces 3 false positive deps-undeclared findings: dijkstra->framework, dijkstra->gamehome-mcp, dijkstra->gamehome-server.

## Suggested fix

When walking source files for any project, build an exclusion list from the other workspace projects' paths (from `workspace.toml`). Pass this exclusion list to `walk_source_files()` (in `dep_validation.py` or `import_scanners.py`) so it skips directories that belong to sibling projects.

This is most critical for `path = "."` projects (where the walk covers the whole repo), but the exclusion logic is correct for any project whose path is a parent of another project's path.

## Effort estimate

Small-medium. The fix is a straightforward change to `walk_source_files()` to accept and apply an exclusion list, plus wiring the workspace project paths into the call site.
