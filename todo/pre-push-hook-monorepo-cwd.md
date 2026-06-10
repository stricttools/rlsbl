# Pre-push hook fails in monorepos: "not inside any registered project"

## Problem

The pre-push hook (`exec rlsbl pre-push-check`) fails in monorepos because git runs hooks from the repo root directory, not from inside a project subdirectory. The hook errors with:

```
Error: CWD is inside monorepo at /home/m/Projects/shopkeep but not inside any registered project.
Run 'rlsbl monorepo add <path>' to register this project.
```

This blocks ALL pushes from the repo, including `rlsbl release run` which tries to `git push` after committing the release.

## Expected behavior

In a monorepo, `rlsbl pre-push-check` should detect it's at the monorepo root and iterate over all registered projects (or the ones with changes being pushed), not fail because the CWD isn't inside a single project.

## Reproduction

```bash
cd ~/Projects/shopkeep  # monorepo root
git push origin main --dry-run
# Error: CWD is inside monorepo but not inside any registered project
```

## Impact

This completely blocks releases via `rlsbl release run` for any monorepo project. The release succeeds (tests, lint, commit, tag) but then fails at the push step because the pre-push hook rejects the push.

## Affected project

shopkeep (~/Projects/shopkeep/) — monorepo with 4 projects (core, crawler, ui, tests)
