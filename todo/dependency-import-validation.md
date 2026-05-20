# Dependency-import validation

## Context

In a monorepo, declared dependencies and actual imports can drift. A package might declare a dependency it doesn't use (dead dep) or import from a sibling it didn't declare (undeclared dep). Both are problems: dead deps create false coupling in the graph, undeclared deps create invisible coupling that breaks when packages are released independently.

rlsbl reads manifests to discover dependencies but never checks whether code actually uses them.

## Decisions

- **Internal to rlsbl, modularized** with a pluggable scanner interface. Not extracted to a separate project.
- **Dart + Python first.** Other languages added incrementally via the scanner interface.
- **Intentional unused-dep whitelist** in a separate config file (`.rlsbl-monorepo/dep-overrides.toml`), not in workspace.toml or package manifests.
- **All checks are errors** (nonzero exit), via the unified strictcli check system.
- **Reuse existing infrastructure**: tree-sitter parsers and regex patterns from `rlsbl/lint/` already extract imports. Refactor them to collect and return the full import set instead of only checking against a forbidden list.

## What the scanner detects

1. **Unused declared deps**: package A declares B as a dependency in its manifest, but no source file in A imports from B. Error unless whitelisted in dep-overrides.toml.
2. **Undeclared imports**: a source file in package A imports from package C, but A does not declare C as a dependency. Always an error.

## Scanner interface

```python
class ImportScanner(Protocol):
    def scan_imports(self, package_dir: str) -> set[str]:
        """Return the set of external package names imported by source files in this directory."""
        ...
```

### Dart scanner

- Scan `.dart` files (including generated ones) for `import 'package:foo/...'` and `export 'package:foo/...'`.
- The `foo` part maps 1:1 to the `name:` field in the dependency's pubspec.yaml (confirmed by research).
- Relative imports (`import '../...'`) are always intra-package -- skip them.
- `dart:` imports are SDK -- skip them.
- Dev dependencies: distinguish `lib/` imports from `test/` imports. `test/` may import dev_dependencies.

### Python scanner

- Refactor `rlsbl/lint/python_ast.py` and `python_regex.py` to return the collected import set.
- Use `ast.parse` to walk `Import` and `ImportFrom` nodes.
- Skip relative imports (`level > 0` -- always intra-package).
- Skip `if TYPE_CHECKING:` imports (typing-only, not runtime deps).
- Include lazy imports (inside functions/methods) -- they are real runtime deps.
- Handle `try: import foo; except ImportError: import bar` -- include both as conditional deps.
- Handle `importlib.import_module('literal')` and `__import__('literal')` -- include when args are string literals, warn on dynamic args.
- Map top-level import names to package names using pypi normalization (`normalize_pypi()`).
- Exclude standard library modules (`sys.stdlib_module_names` on 3.10+).

## Whitelist config

`.rlsbl-monorepo/dep-overrides.toml`:
```toml
[[unused_allowed]]
package = "app"
dep = "auth"
reason = "Wired via DI at runtime, no direct import"

[[unused_allowed]]
package = "app"
dep = "analytics"
reason = "Loaded dynamically via plugin registry"
```

`reason` is mandatory (audit trail).

## Integration

- Two check functions via strictcli check system:
  - `@check(name="deps.unused", scope="workspace", fast=False, pure=True)`
  - `@check(name="deps.undeclared", scope="workspace", fast=False, pure=True)`
- Both should be included in `monorepo lint` / the workspace check group.
- CI should fail on both.

## Edge cases

- **Code generation**: generated files (Drift, Freezed, json_serializable in Dart; dataclasses, pydantic in Python) may produce imports not visible in hand-written source. Scanner reads all source files including generated ones.
- **Transitive imports**: if A depends on B and B re-exports C's types, A might use C's types without importing C directly. A should still declare C if it imports directly from C.
- **Namespace packages** (Python): do not require `__init__.py` to identify packages. Scan for directories with `.py` files.
- **Compiled extensions** (`.so`/`.pyd`): opaque to AST scanning. Rely on declared metadata.

## Affected files

- `rlsbl/lint/python_ast.py`, `rlsbl/lint/python_regex.py` -- refactor to return collected imports
- New: `rlsbl/scanners/import_scanner.py` (interface), `rlsbl/scanners/dart_imports.py`, `rlsbl/scanners/python_imports.py`
- New: config loader for `.rlsbl-monorepo/dep-overrides.toml`
- Check registration in the appropriate module

## Prerequisites

- Cross-language workspace support (scanner interface)
- Unified check system in strictcli

## Effort

Large. Dart scanner is greenfield. Python scanner refactors existing code. The whitelist config and check integration are medium.
