# Scaffold assumes pyproject.toml per monorepo member

## Problem

Monorepo members without `pyproject.toml` (e.g. Go sub-projects, protocol definitions, schema packages) cannot be individually scaffolded. The scaffold command discovers projects by looking for `pyproject.toml` in each member directory, and skips members that lack one.

Downstream consequences:

- The **publish router** does not pick up non-pyproject members, so they have no CI publish workflow.
- The **CI router** does not generate per-member CI workflows for them.
- The **workspace-ci-synced check** (`rlsbl check --tag workspace`) fails because it expects every non-dev-node member to have a CI workflow, but scaffold never created one.

## Root cause

`scaffold` assumes every member is a Python (pypi) target with `pyproject.toml`. The target detection logic (`_detect_target`) returns `None` for directories without a recognized project file, and scaffold silently skips them. The publish and CI routers inherit this assumption.

## Affected

Concrete case: orxtra monorepo has 8 newer members (Go services, protocol definitions, schema packages) that have no `pyproject.toml`. These members are part of the `orxtra` releasable but are invisible to scaffold, publish, and CI infrastructure.

Any monorepo with mixed-language members will hit this.

## Possible solutions

1. **Extend target detection** to recognize `go.mod`, `package.json`, `Cargo.toml`, and bare directories (for non-publishable members like protocol/schema packages). Each target type gets its own scaffold template, publish pipeline, and CI workflow.

2. **Add a `target` field to workspace.toml** per member, so the user declares what kind of project it is. Scaffold reads this instead of auto-detecting. This handles cases where auto-detection is ambiguous or impossible (e.g. a schema package with only `.sql` files).

3. **Hybrid**: auto-detect where possible, fall back to explicit `target` in workspace.toml, hard-error if neither works.

## Effort

Medium-large. Touches scaffold templates, publish router, CI router, target detection, and workspace checks. Each new target type needs its own scaffold template and CI workflow.
