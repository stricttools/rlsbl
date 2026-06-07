---
description: "Monorepo architectural layer enforcement — configuring layer order, assignments, overrides, and validating dependency direction."
---

# Layers

## Overview

Architectural layer rules enforce dependency direction in monorepo workspaces. Layers define a vertical ordering of concerns — foundational libraries at the bottom, application code at the top. The rule is simple: higher layers can depend on lower layers, but lower layers cannot depend on higher layers.

This prevents architectural erosion where foundation code accumulates dependencies on application-specific logic, creating circular dependency chains that make independent releases impossible.

## Configuration

Layers are configured in the `[layers]` section of `.rlsbl-monorepo/workspace.toml`.

### Structure

```toml
[layers]
order = ["foundation", "specs", "contracts", "implementations", "flows", "app"]

[layers.assignments]
foundation = ["schema", "models", "infra"]
specs = ["*_spec"]
implementations = ["payments_*", "shipping_*"]
app = ["app"]

[layers.overrides]
unrestricted = ["conformance", "testing"]
forbidden_targets = ["legacy_*"]

[[layers.overrides.allow]]
source = "schema"
target = "app"
```

### Keys

| Key | Type | Required | Description |
| --- | --- | --- | --- |
| `order` | array of strings | Yes | Layer names from bottom (index 0) to top. Foundational layers first, application layers last. |
| `[layers.assignments]` | table | Yes | Maps each layer name to a list of glob patterns matching project names. |
| `[layers.overrides]` | table | No | Exception rules (see below). |

### Order semantics

The `order` list defines the allowed dependency direction:

- Index 0 is the **bottom** (most foundational)
- Last index is the **top** (most application-specific)
- A project at index N **can** depend on projects at index < N (downward)
- A project at index N **cannot** depend on projects at index > N (upward = violation)
- Projects at the **same** layer index can depend on each other freely

## Assignments

Each layer name in `order` must have a corresponding key in `[layers.assignments]`. The value is an array of glob patterns that match project names (as declared in workspace.toml `name` fields).

```toml
[layers.assignments]
foundation = ["schema", "models", "infra"]
specs = ["*_spec"]
contracts = ["*_contract", "api_*"]
implementations = ["payments_*", "shipping_*", "auth"]
flows = ["checkout_flow", "onboarding_flow"]
app = ["app"]
```

Glob patterns follow standard shell glob rules (`*` matches any characters, `?` matches one character).

## Overrides

The `[layers.overrides]` table provides three exception mechanisms:

| Override | Type | Effect |
| --- | --- | --- |
| `unrestricted` | array of glob patterns | Packages matching these patterns are exempt from all layer checks **as source**. They can depend on anything regardless of layer position. |
| `forbidden_targets` | array of glob patterns | Nothing may depend on packages matching these patterns. Any dependency edge targeting a forbidden package is a violation regardless of layer direction. |
| `[[layers.overrides.allow]]` | array of tables | Explicit exceptions to layer rules. Each entry has `source` and `target` (project names, not globs). |

### When to use each override

| Situation | Override to use |
| --- | --- |
| Test infrastructure that imports from any layer | `unrestricted` |
| Conformance suites that verify all layers | `unrestricted` |
| Deprecated packages being phased out | `forbidden_targets` |
| Legacy code that should not gain new dependents | `forbidden_targets` |
| A specific cross-layer dependency that is architecturally justified | `[[layers.overrides.allow]]` |

### Allow override example

```toml
[[layers.overrides.allow]]
source = "schema"
target = "app"
```

This permits the `schema` project (a lower layer) to depend on `app` (a higher layer). Use sparingly — each allow override is a documented exception that weakens the architectural guarantee.

## Validation

`validate_layer_assignments()` runs as part of layer checking and enforces:

1. Every project in the workspace is assigned to exactly one layer (no gaps)
2. No project matches patterns in multiple layers (no overlaps)
3. Every layer name in `order` has a corresponding key in `assignments`
4. Every key in `assignments` is listed in `order`

Violations produce clear error messages identifying which project is unassigned or multiply-assigned.

## Running the check

The `layers-violations` check is registered under the `workspace` tag:

```bash
# Run just the layers check
rlsbl check --name layers-violations

# Run all workspace checks (includes layers)
rlsbl check --tag workspace
```

The check calls `check_layer_violations()`, which:

1. Loads layer config from workspace.toml
2. Resolves each project's layer assignment via glob matching
3. Walks the workspace dependency graph (declared `depends-on` in workspace.toml)
4. For each dependency edge, checks whether it violates layer ordering
5. Reports each violating edge with source project, target project, source layer, target layer, and direction

A non-empty list of violations causes the check to fail (exit code 1). The output lists each violation so all problems are visible in a single run.

## Decision table

| Source layer index | Target layer index | Override | Result |
| --- | --- | --- | --- |
| Higher | Lower | None | Allowed (downward dependency) |
| Lower | Higher | None | Violation (upward dependency) |
| Same | Same | None | Allowed (same-layer) |
| Any | Any | Source in `unrestricted` | Allowed |
| Any | Forbidden target | None | Violation |
| Lower | Higher | `[[allow]]` with matching source+target | Allowed (explicit exception) |
