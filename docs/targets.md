---
description: "All 17 rlsbl release targets — npm, PyPI, Go, Cargo, Deno, Hex, Maven, Swift, Zig, Docker, Dart, Flutter, docs, and more — with the ReleaseTarget protocol."
---

# Release targets

rlsbl supports 12 release targets, each handling version management and scaffolding for a specific ecosystem.

| Target | Ecosystem | Detection file |
| ------ | --------- | -------------- |
| npm | Node.js / npm | package.json |
| pypi | Python / PyPI | pyproject.toml |
| go | Go modules | go.mod |
| cargo | Rust / crates.io | Cargo.toml |
| deno | Deno | deno.json |
| hex | Elixir / Hex | mix.exs |
| maven | Java / Maven | pom.xml |
| swift | Swift (SPM) | Package.swift |
| swift-apple | Swift (Apple) | *.xcodeproj |
| docker | Docker | Dockerfile |
| docs | Documentation (selfdoc) | selfdoc.json |
| spec | Specification | spec.json |

## Check support matrix

Some checks are universal (they run for any target), while others only apply to targets with language-specific import scanners or AST analysis. This matrix shows which target-specific checks support which targets.

:-: table-feature-matrix

All checks not listed here are universal and run for every target.

## ReleaseTarget protocol

All targets implement the `ReleaseTarget` protocol, a runtime-checkable Python Protocol that defines the required interface for detection, version reading and writing, tag formatting, and scaffolding template resolution. Targets may also provide optional build and publish steps for ecosystems that support automated publishing, such as npm and PyPI.

:-: ref path="rlsbl.targets.protocol"

## Target implementations

Every concrete target extends `BaseTarget`, which provides sensible defaults for optional protocol methods including tag formatting (`v{version}`), monorepo tag formatting (`{name}@v{version}`), shared template mappings for changelogs, licenses, hooks, and lint configs, and no-op stubs for build and publish. Individual targets override only the methods specific to their ecosystem.

:-: ref path="rlsbl.targets.base"
