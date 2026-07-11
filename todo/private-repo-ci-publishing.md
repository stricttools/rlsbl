# Private source repos with CI-automatic public binary-wrapper publishing

## Context

A project shape rlsbl does not currently model: source code in a PRIVATE GitHub repo, but the compiled binary published to public registries (npm, PyPI) as wrapper packages with the binary EMBEDDED (install-time download from a private repo's releases is impossible for end users). Publishing must be fully CI-automatic and scaffold-generated — nothing hand-maintained, nothing published from a local machine — i.e., exactly the standard rlsbl publish.yml flow, just from a private repo shipping binaries only.

## Problem: `private` is one boolean carrying two meanings

`private: true` currently means both "source repo is private" AND "publish nothing to public registries":

- Scaffold skips publish.yml generation entirely for private projects (`rlsbl/commands/init_cmd.py:1436` single-target guard, `:2163` multi-target guard; `_resolve_private` at `:1237-1253` resolves the single boolean; no second axis exists).
- The `private-publish-workflow` check (`rlsbl/checks/project.py:531-568`) hard-fails when a private repo contains ANY workflow whose filename contains "publish" or whose content triggers on release-published — so even hand-adding a workflow is blocked at release time.
- Schema/docs bake in the conflation ("private == suppress publishing": `rlsbl/config.py:187,205`, `rlsbl/targets/__init__.py:256`), and `_print_private_summary` steers private repos to local asset upload.
- Aligned detail worth keeping: `validate_private_config` (`rlsbl/commands/release/validate.py:144-152`) already forbids `local: true` publishing pipelines for private repos — local publishing should stay forbidden; the missing third state is "CI may publish binary artifacts."

## Secondary blockers found in the same investigation

1. **npm `--provenance` fails on private repos.** The pure-npm publish templates hardcode it (`rlsbl/templates/npm/publish.yml.tpl:56`, `publish-pnpm.yml.tpl:57`, `rlsbl/pipelines/npm.py:59`). The Go-to-npm wrapper jobs already omit it (`rlsbl/npm_wrapper.py:142,176`), so only the standalone npm path needs the flag made conditional on repo visibility.
2. **No PyPI embedded-binary wrapper exists.** Templates today: `shared/npm-wrapper/` (embeds binaries in per-platform packages — private-safe, staged in CI via `gh release download` from the repo's own release, `npm_wrapper.py:160-162`) and `shared/crates-wrapper/` (build-time download — NOT private-safe for end users). The only PyPI template (`templates/pypi/publish.yml.tpl`) builds a pure-Python package. Missing: per-platform wheels carrying the binary, built in CI, published via Trusted Publishing (which works from private repos), wired as `{{pypiPublishJobs}}` into `targets/go.py` / `templates/go/publish.yml.tpl` analogous to the npm jobs.
3. **Single-package npm wrapper variant.** The existing npm wrapper model publishes per-platform packages via optionalDependencies — which requires claiming N additional registry names. Some projects decline extra name claims on principle. Needed: a single-package variant under the ONE existing name with the binary embedded directly (initially one platform with `os`/`cpu` fields failing loudly elsewhere; multi-platform later means a fat tarball carrying all binaries with a runtime platform/arch selector shim — the only single-name mechanism npm offers, since tarball selection per platform requires separate packages). The scaffold should let a project choose per-platform or single-package explicitly (mandatory choice, no default, per the agent-experience philosophy).

## Proposed solution sketch

1. Split the axis: new explicit config key (e.g. `publish_when_private: true`, mandatory-absent default = current behavior) threaded through the scaffold guards, the `private-publish-workflow` check (allow scaffold-generated publish workflows when set), and validation/summary messaging. Keep forbidding local publish pipelines for private repos.
2. Make `--provenance` conditional on repo visibility everywhere it is emitted.
3. Add the PyPI embedded-binary wrapper codegen (per-platform wheels with correct platform tags, CI-built, Trusted Publishing).
4. Add the single-package npm wrapper variant (explicit choice between per-platform and single-package shapes).

Version flow (tag-derived in CI) and secret wiring need no changes — verified working for private repos except the provenance flag.

## Affected files

`rlsbl/commands/init_cmd.py`, `rlsbl/checks/project.py`, `rlsbl/commands/release/validate.py`, `rlsbl/config.py`, `rlsbl/targets/__init__.py`, `rlsbl/targets/go.py`, `rlsbl/npm_wrapper.py`, `rlsbl/pipelines/npm.py`, `rlsbl/templates/npm/*.tpl`, new `rlsbl/templates/shared/pypi-wrapper/` (or equivalent), `rlsbl/templates/go/publish.yml.tpl`, docs.

## Effort estimate

Medium-large. Item 1 is small-medium (threading one key through three gates plus messaging). Item 2 is small. Item 3 is the largest (new wheel-building codegen + CI jobs + tests). Item 4 is small-medium (template variant + scaffold choice). Independently landable in that order; item 1 alone unblocks the shape with hand-written-but-check-approved workflows as an interim, though the goal is full scaffold generation.
