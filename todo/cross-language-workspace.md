# Cross-language workspace support

## Context

Three monorepos now use rlsbl with mixed-language workspaces:

- **WWW**: 48 Python packages + 1 Go service + 3 Go/JS/Python SDKs
- **incantino**: Swift + Kotlin + TypeScript + Python, all implementing the same spec
- **F**: 35 Dart packages + 1 Python package + 3 data-only spec packages + 1 Flutter app

rlsbl's workspace graph currently parses pyproject.toml (Python) and package.json (npm). It doesn't parse pubspec.yaml (Dart), Package.swift (Swift), build.gradle.kts (Kotlin), or Cargo.toml (Rust).

## What we need

The workspace graph should discover intra-workspace dependencies from any manifest format rlsbl supports as a target. When a new target type is added (dart, swift, etc.), its manifest parser should be pluggable into the workspace graph's dependency detection.

Current state:
- pypi: reads pyproject.toml `dependencies` and `optional-dependencies` -- works
- npm: reads package.json `dependencies`, `devDependencies`, `peerDependencies` -- works
- go: not parsed for workspace deps
- dart: not parsed (pubspec.yaml)
- swift: not parsed (Package.swift)
- spec: no manifest (uses workspace.toml `depends_on` only)

For cross-language deps (Python tooling reads Dart spec files, Swift app consumes a spec package), `depends_on` in workspace.toml is the only mechanism. This is fine -- cross-language deps can't be auto-detected from manifests.

## Speculative: unified dependency graph across languages

In a workspace where Python tooling generates code consumed by Dart packages, the dep graph spans languages. Today these cross-language edges are manual (`depends_on`). Could rlsbl detect them? For example: Python reads files from `../sdui_spec/`, which is a workspace package -- that's a detectable dependency if rlsbl scans Python imports/file reads.

This is hard and probably not worth automating. Manual `depends_on` is sufficient for cross-language edges.
