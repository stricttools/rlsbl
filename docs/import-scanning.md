---
description: "Import scanning architecture that validates dependencies and detects dead modules using tree-sitter parsers for Python, Go, and JS/TS plus regex for Dart."
---

# Import scanning

rlsbl scans source code imports across 4 language ecosystems to detect unused dependencies, undeclared dependencies, dead modules, and circular dependencies. It uses 3 tree-sitter parsers for Python, Go, and JavaScript/TypeScript parsing, and regex for Dart. Import results are cached per check context to avoid redundant source tree walks across the 4 dependency checks that share scan data.

## Architecture

The import scanning system has two layers, each serving a different purpose. The workspace layer maps imports to project names for dependency validation, while the file layer builds intra-project graphs for dead-module and circular-dependency detection:

### Workspace-level scanners (import_scanners.py)

These scanners parse source files and map discovered import statements to workspace project names. They answer the question "which workspace siblings does this project actually import?" and feed their results to the four dependency validation checks (unused, undeclared, runtime-test-only, dev-in-lib).

- `PythonImportScanner` -- uses `PythonAstLinter.scan_imports()`, filters by workspace membership
- `GoImportScanner` -- uses `scan_imports()` from `lint.go_ast`, matches against workspace Go module paths
- `NpmImportScanner` -- uses `NpmAstLinter.scan_imports()`, extracts bare package names
- `DartImportScanner` -- regex-based extraction of `package:` imports

All scanners return `list[ImportInfo]`, where each `ImportInfo` carries the matched workspace package name, file path, line number, and whether the file is in a test context.

### File-level graph builders (dep_validation.py)

These functions build intra-package import graphs by resolving each import statement to a concrete file path within the same project. They answer the question "which files within this project reference each other?" and produce the adjacency data used by dead-module BFS traversal and circular-dependency detection via Tarjan's algorithm.

- `_build_python_import_graph()` -- implied by `find_dead_modules()` which uses `_collect_python_imports()`
- `find_dead_go_packages()` -- uses `scan_imports()` per file, groups by package directory
- `_build_npm_import_graph()` -- resolves relative imports to absolute file paths
- `_build_dart_import_graph()` -- resolves relative and self-package imports via regex

## Shared infrastructure

### ImportScanner protocol (lint/protocol.py)

```python
class ImportScanner(Protocol):
    def scan_imports(self, project_path: str) -> set[tuple[str, str, int]]:
        """Returns (package_name, file_path, line_number) tuples."""
```

This is the interface that low-level AST linters implement. The workspace-level scanners (above) consume this output and post-process it.

### walk_source_files() (lint/utils.py)

File discovery utility shared by both the workspace-level scanners and the file-level graph builders. It walks the project directory tree, filters by file extension, and excludes non-source directories to produce the set of files that should be scanned for imports. Key features:

- Extension matching (e.g., `(".py",)`, `(".go",)`, `(".js", ".ts", ".mjs", ".cjs", ".tsx")`)
- Built-in exclusion of common non-source directories: `.venv`, `node_modules`, `__pycache__`, `.git`, `build`, `dist`, `.selfdoc`, `_build`, `static`, `public`, `assets`
- Automatic `.egg-info` directory exclusion
- `exclude_patterns` parameter for fnmatch-style glob filtering
- `exclude_dirs` parameter for preventing scans of sibling workspace project directories (critical for root-path monorepo projects where sibling project dirs are immediate children)

### _is_test_context()

Classifies a file as production vs test code by checking its path against known test directory names and file naming conventions. This classification determines whether an import counts toward runtime dependency usage or test-only usage, which directly affects the deps-runtime-test-only and deps-dev-in-lib checks. Classification is based on:

- **Directory names**: `test`, `tests`, `__tests__`, `examples`, `example`
- **File name patterns**: `test_*.py`, `*_test.py`, `*_test.go`, `*_test.dart`, `*.test.[jt]sx?`, `*.spec.[jt]sx?`, `conftest.py`

### _NON_PRODUCTION_PATTERNS

Shared constant exposing the file classification patterns as a dict with 3 keys: `test_dirs` (5 directory names like `test`, `tests`, `__tests__`), `example_dirs` (2 directory names), and `test_file_patterns` (7 glob patterns for test file naming conventions). This dict is reused by both `import_scanners.py` and `dep_validation.py` to keep production vs test classification consistent across all dependency checks.

## Per-language details

| Language | Parser | Workspace Scanner | Graph Builder | Exclusions |
|----------|--------|-------------------|---------------|------------|
| Python | tree-sitter-python | PythonImportScanner | `_collect_python_imports()` + `find_dead_modules()` | stdlib (`sys.stdlib_module_names`), relative imports |
| Go | tree-sitter-go | GoImportScanner | `find_dead_go_packages()` via `scan_imports()` | self-module imports |
| npm (JS/TS) | tree-sitter-javascript + tree-sitter-typescript | NpmImportScanner | `_build_npm_import_graph()` | Node.js builtins, relative imports |
| Dart | regex | DartImportScanner | `_build_dart_import_graph()` | `dart:` imports, external `package:` imports |

## Go module path mapping

`GoImportScanner` handles Go's module-path-based import system by building a reverse lookup from Go module paths to workspace project names. This mapping is necessary because Go imports use full module paths like `github.com/org/repo/pkg`, not bare package names:

1. Reads `go.mod` from each workspace project to extract its `module` declaration
2. Builds a `module_path_map: dict[str, str]` mapping workspace project name to module path
3. For each import in source files, checks if the import path equals or starts with (+ `/`) any workspace module path
4. Excludes self-imports by reading the scanning project's own module path

This handles Go's module-path-based import system where `github.com/org/repo/internal/pkg` maps to a workspace project whose `go.mod` declares `module github.com/org/repo`.

## npm resolution

The npm file-level graph builder (`_build_npm_import_graph()`) implements a subset of Node.js module resolution to accurately map import statements to source files. This is necessary because JavaScript and TypeScript have several implicit resolution conventions that affect which file an import actually refers to:

- **Extension appending**: tries `.ts`, `.tsx`, `.js`, `.mjs`, `.cjs` when bare path has no extension
- **`.js` to `.ts` mapping**: TypeScript projects compile `.ts` to `.js`; resolves `.js` references back to `.ts` source
- **Directory to index file**: resolves `./utils` to `./utils/index.ts` (tries `index.ts`, `index.tsx`, `index.js`, `index.mjs`, `index.cjs`)
- **Import types**: ES6 `import`, CommonJS `require()`, dynamic `import()`
- **Conditional exports**: `_collect_export_paths()` recursively traverses `package.json` `exports` maps (string, dict with condition keys, nested subpath maps, arrays)

## Caching

Workspace-level import results are cached on the check context object (`ctx._dep_import_cache`) to avoid scanning the same source trees multiple times. Since four separate checks all need the same import data, caching reduces the total number of source tree walks from four per project to one. The `_build_dep_import_cache()` function in `checks.py`:

1. Iterates all workspace projects once
2. Computes `(lib_imports, test_imports)` per project using `_get_imported_workspace_packages()`
3. Stores the result dict on `ctx._dep_import_cache`
4. Returns the cached result on subsequent calls

This cache is shared across four workspace dependency checks:

- `deps-unused`
- `deps-undeclared`
- `deps-runtime-test-only`
- `deps-dev-in-lib`

The intra-package checks (`dead-modules`, `circular-deps`) build their own file-level graphs using the same underlying `walk_source_files()` and AST infrastructure, but do not share the workspace cache since they operate at a different granularity (individual files rather than workspace package names).

## Entry point detection

Dead-module analysis requires knowing which files serve as roots for BFS reachability traversal. A file is considered an entry point if it is part of the package's public API or an executable script that users invoke directly. Entry point detection varies by language because each ecosystem has different conventions for declaring public surfaces:

| Language | Entry points |
|----------|-------------|
| Python | `__init__.py` files (package entry points); all production modules cross-reference each other via import prefix matching |
| Go | Internal packages only -- checks whether any non-test file outside the package directory imports the package path |
| npm | `package.json` fields: `exports` (recursive path collection), `main`, `bin` (string or dict of paths) |
| Dart | `lib/<package_name>.dart` (barrel file from `pubspec.yaml` name field) + all `bin/*.dart` scripts |

## Source modules

The import scanning implementation spans 3 modules: `import_scanners` provides the per-language AST parsers and workspace-level import collection, `lint.protocol` defines the shared interface for lint rule implementations, and `lint.utils` contains utility functions for file walking, pattern matching, and result aggregation.

:-: ref path="rlsbl.import_scanners" lang="python"

:-: ref path="rlsbl.lint.protocol" lang="python"

:-: ref path="rlsbl.lint.utils" lang="python"
