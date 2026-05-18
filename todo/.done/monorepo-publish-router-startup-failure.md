# Monorepo publish router: startup_failure with reusable workflows

## Problem

The monorepo scaffold generates a publish router (`publish.yml`) that calls a per-project reusable workflow:

```yaml
jobs:
  strictcli:
    if: startsWith(github.event.release.tag_name, 'strictcli@v')
    uses: ./.github/workflows/strictcli-publish.yml
```

This consistently produces `startup_failure` on GitHub Actions when triggered by release events. Zero jobs run — the workflow never starts.

Both `publish.yml` and `strictcli-publish.yml` exist at the tagged commit, are valid YAML, and reference actions that exist. The CI router uses the same `uses: ./.github/workflows/` pattern for push/PR events and works fine. The failure is specific to release events.

## Workaround

Inlining the publish jobs directly into `publish.yml` (no reusable workflow reference) resolves the issue. This was done manually for strictcli.

## Proposed fix

The scaffold should generate publish routers with inlined jobs instead of reusable workflow references. Alternatively, investigate why GitHub's `uses:` resolution fails specifically for `release: [published]` events — there may be a ref resolution difference.

## Affected

All monorepo projects with publish workflows. Discovered in strictcli (3 consecutive startup_failures across different releases before the inline fix worked).
