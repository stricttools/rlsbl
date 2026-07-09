# Publish router CI_CHECK_REGEX strips `-pypi` from CI workflow filenames

## Problem

When a monorepo member's CI source file is named `ci-pypi.yml` (not `ci.yml`), `rlsbl monorepo sync` generates the publish router's `CI_CHECK_REGEX` using the project name + `-ci` prefix (e.g., `selfdoc-ci`), but the actual CI check-run name prefix includes the full filename minus `.yml`: `selfdoc-ci-pypi`.

The publish gate regex `^(selfdoc\-ci) / ` does not match the actual check-run name `selfdoc-ci-pypi / test (3.14)`, causing the gate to time out and refuse to publish.

## Affected code

`rlsbl/commands/monorepo/publish_inline.py` — the function that computes CI job keys from CI workflow filenames.

## Impact

Any monorepo member with a CI file named anything other than `ci.yml` (e.g., `ci-pypi.yml`, `ci-go.yml`) gets a broken publish gate regex. The selfdoc monorepo has two such members (selfdoc with `ci-pypi.yml`, selfblog with `ci-pypi.yml`).

## Current workaround

Manual edit of `publish.yml` after `monorepo sync`. Gets overwritten on next sync.

## Fix

The CI job key computation should use the full filename minus `.yml` extension, not strip to `{name}-ci`.
