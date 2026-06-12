# npm CI template fails when project has no package-lock.json

## Problem

The `ci-npm.yml` scaffold template uses `npm ci`, which strictly requires a `package-lock.json`. Projects with zero npm dependencies (e.g., Python packages that dual-publish to npm as a thin wrapper) have no lock file and never will. The CI fails on every push with:

```
npm error: The npm ci command can only install with an existing package-lock.json
```

This affects claudewheel and potentially any project that uses npm as a distribution channel without having actual npm dependencies.

## Proposed fix

Either:
1. Replace `npm ci` with `npm install` in the template — works with or without a lock file
2. Add a conditional: if `package-lock.json` exists, use `npm ci`; otherwise use `npm install` or skip the install step entirely
3. For projects with no `devDependencies` and no `dependencies`, skip the npm install step entirely since there's nothing to install

## Additional consideration

The npm CI template runs `npm test`, which for these wrapper projects just delegates to `python3 -m pytest tests/` — the same tests that `ci-pypi.yml` already runs. The npm CI could instead focus on npm-specific concerns (can the package be packed? does the bin script exist?) rather than redundantly running Python tests.
