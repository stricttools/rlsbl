# Architectural layer rules

## Context

Monorepos with layered architectures (foundation, specs, contracts, implementations, flows, app shell) have implicit dependency direction rules: flows depend on contracts, contracts depend on models, models depend on schema. These rules are documented in prose but not enforced. Nothing prevents an upward dependency or a layer bypass.

## Decisions

- **Both named layers and glob-based overrides.** Named layers define a strict ordering (e.g., `foundation < contracts < implementations < flows < app`). Glob-based rules handle exceptions (e.g., "testing can depend on anything", "legacy packages are forbidden targets").
- **Config lives in workspace.toml** under a `[layers]` section. Prerequisite: fix `save_workspace()` to round-trip unknown top-level keys (read-then-modify with tomlkit).
- **Hard errors.** Violations fail the check with nonzero exit. No configurable severity per rule.
- **Enforcement via the unified check system** (strictcli `@check` decorator). The check reads the layer config and the workspace dependency graph, then validates all edges against the rules.

## Config format

```toml
[layers]
order = ["foundation", "specs", "contracts", "implementations", "flows", "app"]

[layers.assignments]
foundation = ["schema", "models", "infra"]
specs = ["*_spec"]
contracts = ["*_contract"]
implementations = ["marketplace", "payments_*", "shipping_*"]
flows = ["flow_*"]
app = ["app"]

[layers.overrides]
# Packages that can depend on anything (test infrastructure)
unrestricted = ["conformance", "testing"]
# Packages that nothing may depend on
forbidden_targets = ["legacy_*"]
# Specific cross-layer allowances
allow = [
    { source = "app", target = "*" },  # app is the composition root
]
```

## Implementation

- A new check function registered with `@check(name="layers.violations", scope="workspace", fast=True, pure=True)`.
- Reads layer config from workspace.toml.
- Builds the full workspace dependency graph.
- For each edge (A depends on B): resolve both to their layer, verify the edge flows downward (or is explicitly allowed by an override).
- Reports all violations with package names and the rule they break.

## Prerequisites

- `save_workspace()` round-trip fix (tomlkit read-then-modify)
- Unified check system in strictcli (see strictcli todo: `check-system.md`)
- Workspace graph must be available to checks (the check needs a `WorkspaceGraph` instance)

## Affected files

- `rlsbl/workspace.py` -- load/save the new `[layers]` section
- `rlsbl/commands/monorepo.py` or a new `rlsbl/checks/` module -- the check function
- `rlsbl/workspace_graph.py` -- may need a `has_edge(a, b)` method for efficient queries

## Effort

Medium. The check logic itself is straightforward (iterate edges, check layers). The config format design and workspace.toml round-tripping are the main work.
