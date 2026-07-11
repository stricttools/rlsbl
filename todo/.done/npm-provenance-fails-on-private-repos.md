# npm publish scaffold hardcodes --provenance, which hard-fails on private repos

## Context

The scaffolded `publish.yml` runs `npm publish --provenance --access public` in the npm job. Sigstore provenance verification requires the GitHub Actions source repository to be public. On a private repo, the npm registry rejects the PUT with:

```
npm error code E422
npm error 422 Unprocessable Entity - PUT https://registry.npmjs.org/<pkg> -
Error verifying sigstore provenance bundle: Unsupported GitHub Actions source
repository visibility: "private". Only public source repositories are supported
when publishing with provenance.
```

## Problem

A consumer project with a private repo and an npm target gets a release whose git side and other registry targets succeed while the npm job fails deterministically — every retry included. The failure surfaces only at publish time (after tag/push/GitHub Release), the worst possible moment. Nothing at scaffold time or release preflight warns that npm + private repo + provenance cannot work.

Because `publish.yml` is scaffold-managed, hand-editing `--provenance` out is non-durable (a re-scaffold three-way merge may reintroduce or conflict) and invisible to the tool.

## Possible solutions

1. **Preflight check.** `rlsbl check` (preflight tag) and `rlsbl release run` verify: if targets include npm AND the workflow uses `--provenance`, the repo must be public (`gh repo view --json isPrivate`). Hard error with the three options spelled out (make repo public / disable provenance / drop npm target). Pros: fails before the tag/push, message is actionable; no behavior change for correct setups. Cons: needs `gh` at preflight (already a release prerequisite).
2. **Scaffold-time decision.** Scaffold asks (or reads a config key like `npm_provenance = true|false`, mandatory when targets include npm) and emits the workflow with or without `--provenance` accordingly. No implicit default — the consumer must declare. Pros: aligns with "mandatory flags over defaults"; the workflow matches reality from day one. Cons: existing scaffolds need a migration; visibility can change after scaffold time (option 1 still needed as the runtime guard).
3. **Both** (recommended combination): mandatory config key at scaffold time + preflight guard that validates the declared choice against actual repo visibility.

Explicitly rejected by house rules: auto-detecting visibility in the workflow and silently skipping `--provenance` (silent runtime degradation — same input must produce same behavior).

## Affected files

- scaffold template for `publish.yml` (npm job)
- preflight/check registration
- config schema (if option 2/3)
- docs for the npm target

## Effort

- Option 1: small (~1 h with tests)
- Option 2: medium (scaffold + migration path)
- Option 3: medium
