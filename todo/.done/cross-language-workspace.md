# Cross-language workspace support

## Context

Monorepos increasingly mix languages: Python + Go + JS SDKs, Swift + Kotlin + TypeScript + Python implementing the same spec, Dart + Python + data-only spec packages. rlsbl's workspace graph currently only parses `pyproject.toml` (Python) and `package.json` (npm) for intra-workspace dependencies. Other manifest formats are not parsed.

## Decisions

- **Pluggable scanner interface first**, then add parsers incrementally.
- **Dart + Python first** (immediate needs), then Cargo, Go, Swift, Kotlin.
- **Pub workspace support**: new `resolution: workspace` pattern only. No legacy `path:` dep support.
- **Cargo**: resolve `workspace = true` inheritance by reading root `[workspace.dependencies]`.
- **Kotlin/Gradle**: parse `settings.gradle.kts` for module discovery + `build.gradle.kts` for dependency declarations.
- **Cross-language edges** stay manual via `depends_on` in workspace.toml. Auto-detection across languages is not worth the complexity.

## Implementation

### Phase 1: pluggable scanner interface

Refactor `workspace_graph.py` to replace the hardcoded `_scan_pypi()` / `_scan_npm()` calls with a scanner registry:

```python
class WorkspaceScanner(Protocol):
    def scan(self, project_dir: str, workspace_names: set[str]) -> list[Dependency]:
        ...

SCANNERS: list[WorkspaceScanner] = [
    PypiScanner(),
    NpmScanner(),
]
```

The `WorkspaceGraph.__init__()` loop becomes:
```python
for scanner in SCANNERS:
    found_deps.extend(scanner.scan(project_dir, workspace_names))
```

### Phase 2: Dart scanner (pubspec.yaml)

- Detect `resolution: workspace` in pubspec.yaml
- When present, read `dependencies` section -- hosted deps whose names match workspace members are intra-workspace deps
- Version constraints in the manifest are noted as `dep_type="versioned"` (same as pypi/npm)
- Parser must understand pubspec.yaml YAML format (use PyYAML or ruamel.yaml)

### Phase 3+: additional scanners

Each as a separate release:

| Scanner | Manifest | Dep detection |
|---------|----------|---------------|
| Cargo | `Cargo.toml` | Path deps (`path = "../sibling"`), workspace-inherited deps (`workspace = true` resolved from root `[workspace.dependencies]`) |
| Go | `go.mod` | `require` directives matching workspace module paths |
| Swift | `Package.swift` | `.package(path: "../sibling")` via regex (Swift source, not a data format) |
| Kotlin | `settings.gradle.kts` for discovery, `build.gradle.kts` for deps | `project(":module")` references |

## Affected files

- `rlsbl/workspace_graph.py` -- refactor to scanner interface, existing pypi/npm scanners become `PypiScanner`/`NpmScanner`
- New files per scanner: `rlsbl/scanners/dart.py`, `rlsbl/scanners/cargo.py`, etc. (or a `scanners/` directory)
- `rlsbl/targets/utils.py` -- `normalize_pypi()` stays; each scanner handles its own normalization

## Effort

Phase 1 (interface refactor): small. Phase 2 (Dart): medium. Phase 3+ (each additional scanner): small per scanner.
