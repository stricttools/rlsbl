# Pipeline `dir` field: allow npm/pypi pipelines to build/publish from a subdirectory

## Context

A Go project (private source, binaries published as registry wrappers) needs to ship npm and PyPI packages whose contents are built in a `packaging/` subdirectory: a thin launcher plus an embedded binary, one package name per registry. This is the same shape rlsbl itself uses for its own npm wrapper distribution.

## Problem

rlsbl's npm/pypi targets and pipelines are root-anchored:

- Target auto-detection only recognizes root-level manifests (`package.json`, `pyproject.toml`). A wrapper manifest in `packaging/npm/` or `packaging/pypi/` is invisible.
- The built-in npm/pypi pipelines run `npm publish` / `uv build` + `uv publish` at the project root. They cannot be pointed at a subdirectory.
- Placing wrapper manifests at the project root to satisfy detection would misrepresent the project (a Go project would be detected as npm+pypi) and pollute version bumping.

Consequence: binary-wrapper packages have no first-class home in the release flow. The workaround is a `pre_release` config hook that builds artifacts into `dist/` (which does correctly pass through the secret-scan gate) plus a manual or post-release-hook `npm publish dist/*.tgz` / `uv publish dist/*.whl` — publish steps living outside the pipeline model, invisible to rlsbl's target bookkeeping.

## Possible solutions

1. **`dir` field on pipeline entries** (mirroring the `dir` field hook entries already support): a pipeline entry like `{"target": "npm", "dir": "packaging/npm"}` runs manifest detection, version injection, build, and publish inside that directory. Pros: smallest conceptual change, reuses existing pipeline code, consistent with hooks. Cons: target auto-detection semantics need a rule for explicit-dir entries (skip auto-detection for them).
2. **First-class `wrapper` target type**: a target that declares `builds_from` (subdir) and `publishes` (registry), with version injected from the release version. Pros: models intent precisely (wrapper-of-a-binary), could standardize the embed pattern rlsbl itself uses. Cons: new target type, more surface.
3. **Documented post-release publish contract**: keep builds in pre_release hooks, add a sanctioned `publish_artifacts` config key (registry → glob) executed after the GitHub release, with the same no-bypass discipline. Pros: no pipeline changes. Cons: publishing remains outside pipelines/targets, version consistency checks don't cover it.

Option 1 is the most idiomatic; option 3 is the cheapest stopgap and could coexist.

## Additional friction found in the same investigation

- Pre-release hooks see the new version only via the `RLSBL_VERSION` env var (the VERSION file is not yet bumped when they run); post-release hooks see it in files. This asymmetry is undocumented and easy to get wrong.
- `custom_assets` is GitHub-Release-only (staged, uploaded, deleted) and cannot feed registry publishing, despite being the closest existing "build artifacts at release time" primitive.
- The pre-publish secret scan scans ALL artifacts present in `dist/` (including stale ones from older versions), not just the ones belonging to the release being cut.

## Affected files (from reading the source)

- `rlsbl/targets/npm.py` (root-anchored detection)
- pipeline execution for npm/pypi targets (root-anchored publish)
- `execute.py` around the secret-scan call site (stale-artifact scoping, if addressed)
- config schema + docs for the new field/key

## Effort estimate

Option 1: small-to-medium — plumb a `dir` through detection/build/publish for the two registry pipelines plus tests. Option 3: small. Documentation fix for the RLSBL_VERSION asymmetry: trivial, worth doing regardless.
