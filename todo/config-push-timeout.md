# Support push_timeout in project config

## Problem

The default push timeout is 120s (configurable via `RLSBL_PUSH_TIMEOUT` env var). Projects with pre-push hooks that run test suites (e.g., conformance checks taking ~70s) regularly exceed this during releases, where the pre-push hook re-runs checks that the pre-release hook already passed.

## Suggestion

Add `push_timeout` to `.rlsbl/config.json` (or `workspace.toml` for monorepos) so the timeout is project-level configuration, not an env var that must be set manually or in hook scripts.

```json
{
  "push_timeout": 300
}
```

## Current workaround

`RLSBL_PUSH_TIMEOUT=300 rlsbl release run --watch --yes` on every release invocation.
