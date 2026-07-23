# npm publish job runs without installing devDependencies

## Context

The scaffolded/generated npm publish workflow job runs `npm publish` directly,
without any dependency installation step. `npm publish` triggers the package's
`prepack` lifecycle script. For packages whose `prepack` compiles sources
(e.g., `npm run build` -> `tsc`), the build toolchain lives in
`devDependencies` (typescript, `@types/node`, etc.) -- and since the job never
runs `npm ci`, none of it is present in the CI checkout.

## Problem

Any npm package whose `prepack` script builds fails at publish time with a
missing dev toolchain. Observed verbatim in a real publish run:

```
error TS2688: Cannot find type definition file for 'node'.
```

The git/tag/GitHub-Release side of the release completes, but the registry
publish fails, leaving the release half-done and requiring manual recovery.

This path was never exercised before because previously published
wrapper-style npm packages had no build step: their `prepack` (if any) did not
invoke a compiler, so publishing from a bare checkout worked by accident. The
first package with a real `prepack` build hit the gap immediately.

## Proposed fix

The generated npm publish job should run a full `npm ci` (not
`npm ci --omit=dev`) before `npm publish`, so devDependencies are available to
lifecycle scripts.

Pros:
- One-line-ish template change; matches what every CI job that builds already
  does.
- `npm ci` is lockfile-strict and reproducible; no risk of drifting installs.

Cons / considerations:
- Slightly slower publish job for packages that don't need it (mitigable with
  actions/setup-node npm caching, but plain `npm ci` is correct and simple).
- Re-scaffolding consumers is needed for the fix to reach existing repos'
  publish workflows (three-way merge should handle it).

## Affected files

- The npm publish workflow template used by scaffolding (the source of the
  generated `publish.yml` npm job).
- Scaffold bases/three-way-merge inputs for existing consumers.

## Effort estimate

Small: ~15-30 minutes. Add `npm ci` to the npm publish job template, update
any golden/template tests, re-scaffold a consumer to verify the merged
workflow publishes a package with a building `prepack`.
