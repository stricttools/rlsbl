# Publish Router shows FAILED for plain targets

## Problem

When a monorepo sub-project with `targets = ["plain"]` is released, the Publish Router workflow triggers but has no matching publish job. It shows as FAILED in GitHub Actions, and `rlsbl watch` reports it as a failure.

This is cosmetic -- the CI Router (which runs tests) passes fine. But the false failure in `rlsbl watch` output is confusing and masks real issues.

## Observed in

strictcli monorepo, conformance sub-project (targets: plain). Every conformance release (0.3.0, 0.3.1, 0.3.2) shows Publish Router FAILED.

## Options

1. **Publish workflow skips plain targets**: The publish router should detect plain targets and skip entirely (or show as "skipped" not "failed").
2. **rlsbl watch ignores publish for plain targets**: `rlsbl watch` could read the project config and not report publish workflow status for plain targets.
3. **No-op publish job**: The publish router emits a no-op job for plain targets that succeeds immediately.

## Effort

Small. Likely a conditional in the publish router template or in `rlsbl watch`.
