# Scaffold: inconsistent action versions across generated workflows

## Problem

`rlsbl scaffold --update` generates workflows with different action versions depending on which template was last updated:

- Publish workflows get latest versions (e.g., `actions/checkout@v6`, `actions/setup-node@v6`)
- CI router still uses older versions (e.g., `actions/checkout@v4`, `dorny/paths-filter@v3`)

This means running `scaffold --update` doesn't bring all generated workflows up to the same action versions. Users must manually update the CI router.

## Context

GitHub is deprecating Node.js 20 actions on June 2, 2026, forcing migration to Node.js 24. Projects need `actions/checkout@v6`, `dorny/paths-filter@v4`, etc. The scaffold should generate these versions consistently.

## Proposed fix

Maintain a central action version table in rlsbl's templates. When `scaffold --update` runs, all generated workflows (CI, publish, routers) should use the same action versions from this table.

## Affected

All scaffold-generated projects, especially monorepos where routers and per-project workflows are generated separately.
