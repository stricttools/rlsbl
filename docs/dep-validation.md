---
description: "Dependency validation checks for monorepo workspaces: unused deps, undeclared deps, dead modules, and circular dependency detection."
---

# Dependency validation

rlsbl validates dependencies at two levels: workspace-level (between monorepo packages) and file-level (within a single package). Four workspace checks detect mismatches between declared and actual dependencies across all 4 supported language ecosystems. Three intra-package checks detect dead code and import cycles using per-file BFS traversal and Tarjan's algorithm for strongly connected components.

## Workspace dependency checks

These four checks compare declared dependencies in workspace manifests against actual import usage discovered by scanning source code. They share a common import scan cache (`ctx._dep_import_cache`) to avoid redundant source tree walks, and all four run during `rlsbl check --tag workspace`.

### deps-unused

Detects workspace dependencies declared in the project manifest that are not actually imported anywhere in the project's source code. An unused dependency adds unnecessary coupling and bloats the install footprint for consumers.

- Scans both lib and test code for imports matching workspace sibling names
- A dep is unused if zero source files import it (regardless of context)
- Supports overrides via `.rlsbl-monorepo/dep-overrides.toml` with mandatory `reason` field
- Override format: `[[unused_allowed]]` entries with `package`, `dep`, and `reason` keys

### deps-undeclared

Detects imports of workspace sibling packages that are not declared as dependencies in the project's manifest. Undeclared dependencies work locally because all packages share a repository, but break when the project is installed standalone.

- Only checks production code (non-test context) -- test files have more lenient rules
- Self-imports are excluded (a package importing its own submodules is fine)
- Detected import must match a workspace sibling name to trigger

### deps-runtime-test-only

Detects runtime dependencies that are only imported in test code and never in production source files. These should be declared as dev dependencies instead, since shipping them as runtime dependencies forces consumers to install packages they will never use.

- Checks deps with `scope="runtime"` in the workspace manifest
- Flags any runtime dep that appears in `test_imports` but not in `lib_imports`
- Helps maintain correct dependency scoping for published packages

### deps-dev-in-lib

Detects dev dependencies that are imported in production code, indicating they should be declared as runtime dependencies instead. When a dev dependency is used in library or application code, consumers who install the package will get import errors because dev dependencies are not installed transitively.

- Checks deps with `scope="dev"` in the workspace manifest
- Flags any dev dep that appears in `lib_imports`
- Catches incorrect scoping that would break consumers at install time

## Intra-package checks

These checks build per-file import graphs within a single project to detect structural problems like unreachable code and circular dependencies. They operate at file granularity rather than package granularity and do not share the workspace import cache.

### dead-modules

Detects source files that are unreachable from any entry point via BFS on the file-level import graph. Dead modules are production code that can never be executed because no import chain connects them to the package's public API or executable entry points.

**Algorithm:**

1. Identify entry points (language-specific, see table below)
2. Build file-level import graph (each file maps to the set of files it imports)
3. BFS from all entry points
4. Report files in production code that are never reached

**Exclusions (common to all languages):**

- Test files (matching `_NON_PRODUCTION_PATTERNS`)
- `.selfdoc` directories
- `_build` directories
- Browser asset directories (`static`, `public`, `assets`)
- Generated files (`.g.dart` for Dart)

**Per-language behavior:**

| Language | Detection function | Entry points | Scope |
|----------|-------------------|--------------|-------|
| Python | `find_dead_modules()` | `__init__.py` files + cross-reference via import prefix matching | All production `.py` files |
| Go | `find_dead_go_packages()` | Packages imported by any non-test file outside the package | Only `internal/` packages (Go enforces visibility elsewhere) |
| npm | `find_dead_npm_modules()` | `package.json` exports/main/bin fields, resolved to source | All production `.js`/`.ts`/`.mjs`/`.cjs`/`.tsx` files |
| Dart | `find_dead_dart_modules()` | `lib/<name>.dart` barrel file + `bin/*.dart` scripts | All production `.dart` files |

### circular-deps

Strongly connected components (Tarjan's algorithm) in the file-level import graph. Only cycles with 2+ nodes are reported (self-loops are not flagged).

**Severity by language:**

| Language | Severity | Rationale |
|----------|----------|-----------|
| npm (JS/TS) | error (fail) | Circular imports cause runtime issues (undefined values, initialization order bugs) |
| Python | warning | Python handles circular imports at runtime but they indicate design problems |
| Dart | warning | Dart handles cycles but they indicate poor layering |
| Go | excluded | The Go compiler rejects circular imports natively; rlsbl does not duplicate that check |

### library-lint

Enforces quality constraints specific to library packages by detecting imports and patterns that are inappropriate for reusable code consumed by other projects:

- Detects imports inappropriate for library code (e.g., `dart:io` in a pure Dart library)
- Detects stdout/stderr writes in library code (libraries should not print directly)
- Only applies to projects marked `library = true` in workspace.toml

## Dead workspace packages

The `dead-workspace-packages` check operates at the workspace level, identifying library packages that no workspace sibling imports. A library with zero importers may indicate abandoned code, an incomplete migration, or a package that is only consumed by external projects outside the monorepo.

**Criteria:**

- Only checks projects with `library = true` (apps/CLIs are entry points, not consumed)
- Skips `dev_node = true` projects
- Checks both lib and test import contexts from all other workspace projects

**Results:**

| Condition | Severity | Message |
|-----------|----------|---------|
| Library imported in production code by at least one sibling | pass (alive) | -- |
| Library only imported in test code | warn | "library 'X' is only imported in test code by workspace siblings (A, B)" |
| Library not imported by any workspace package | warn | "library 'X' is not imported by any workspace package" |

Published libraries may still be consumed externally, so zero workspace importers is a warning, not an error.

## Language support matrix

:-: table-feature-matrix

## Configuration

### dep-overrides.toml

Located at `.rlsbl-monorepo/dep-overrides.toml`, this configuration file allows suppressing `deps-unused` errors for dependencies that are intentionally declared but not statically detectable by import scanning (e.g., dynamic imports, plugin loading, or runtime reflection).

```toml
[[unused_allowed]]
package = "my-app"
dep = "my-lib"
reason = "Used via dynamic import at runtime, not statically detectable"
```

Every entry requires all three fields (`package`, `dep`, `reason`). Empty `reason` values are rejected. This ensures the override has a documented justification.

### batch_limits in config.json

Controls batch size validation for changelog entries, limiting how many commits a single entry can reference and how many entries can reference the same commit. While not directly related to dependency validation, these limits are part of the same check infrastructure and run alongside dep checks during `rlsbl check`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_commits_per_entry` | int | 5 | Maximum commit hashes allowed in a single JSONL entry |
| `max_entries_per_commit` | int | 5 | Maximum JSONL entries that may reference the same commit |
| `exclusions` | array | `[]` | Per-violation silencers, each with mandatory `reason` plus `commits` or `entries` |

## Source modules

:-: ref path="rlsbl.dep_validation" lang="python"

:-: ref path="rlsbl.import_scanners" lang="python"
