# Private source repos publishing public binary wrappers: fix the misleading `private` semantics and close the remaining gaps

## Correction notice (this todo was updated in place)

The original version of this todo claimed the shape "private GitHub repo + CI-automatic public registry publishing" is unsupported. That premise is WRONG: a project in the fleet already does exactly this today — its GitHub repo is private while its `.rlsbl/config.json` says `"private": false`, and its scaffold-generated publish.yml (npm + PyPI, release-triggered, CI-gated, no `--provenance`) has published successfully to both registries. Verified: the `private-publish-workflow` check passes because it reads only the config key (`rlsbl/checks/project.py:539-540`), and nothing anywhere compares the config key to actual GitHub visibility after init.

So the shape works. What remains are real but smaller issues:

## Problem 1: the `private` key's name actively misleads

`private` semantically means "suppress publishing," but it reads as "repo is private on GitHub," and `_resolve_private` (`rlsbl/commands/init_cmd.py:1237-1253`) seeds it from the GitHub API at init — so a private repo that WANTS to publish gets `private: true` scaffolded automatically and publish.yml silently skipped (`init_cmd.py:1436`, `:2163`). The operator must then know to counterintuitively flip the key to `false` on a repo that is, in fact, private. Schema texts reinforce the conflation (`rlsbl/config.py:187,205`, `rlsbl/targets/__init__.py:256`, `_print_private_summary` at `init_cmd.py:1283-1287`).

Fix options:
1. **Rename the key to what it means** (e.g. `suppress_publishing` or `publish: false`), with config migration and updated auto-detect messaging ("repo is private on GitHub; defaulting publish to off — set publish=true to ship binary artifacts from CI"). Most correct.
2. Keep the key, but make init's auto-detect interactive/explicit for private repos (mandatory choice: "private repo: publish nothing / publish binaries from CI") per the mandatory-flags-over-defaults philosophy, and fix the schema/docs wording.

Either way, the goal is that "private source, public binary artifacts" is a first-class declared choice, not a knowingly-mislabeled boolean.

## Problem 2: `--provenance` inconsistency in npm templates

`rlsbl/templates/npm/publish.yml.tpl:56`, `templates/npm/publish-pnpm.yml.tpl:57`, and `rlsbl/pipelines/npm.py:59` hardcode `npm publish --provenance`, which fails on private repos. Yet the fleet project's generated publish.yml has NO provenance flag, and the Go-to-npm wrapper jobs omit it too (`rlsbl/npm_wrapper.py:142,176`). Reconcile: determine which generation path drops it, then make provenance conditional (config-declared or visibility-derived) so the pure-npm path cannot generate a workflow that fails on private repos.

## Problem 3: no embedded-binary PyPI wrapper codegen

Existing wrapper codegen: `templates/shared/npm-wrapper/` (per-platform packages, binaries embedded in the package tarballs, staged in CI via `gh release download` — private-safe) and `templates/shared/crates-wrapper/` (build-time download at the END USER's machine — not private-safe). For PyPI there is only the pure-Python template (`templates/pypi/publish.yml.tpl`). Missing: per-platform wheels carrying the binary, built in CI (staged from the repo's own release like the npm wrapper does), published via Trusted Publishing, wired as publish jobs analogous to `build_npm_publish_jobs` (`rlsbl/npm_wrapper.py:98,150`, `rlsbl/targets/go.py:268-275,304`).

Note: subdirectory wrapper targets already work via explicit target `path` entries (e.g. `{"name": "pypi", "path": "pypi/"}`), so a project-side wrapper in a subdir is viable today; this item is about scaffold-generated codegen for the EMBEDDED-wheel shape specifically (the generated pypi job must arrange for the binary to be present before the wheel build — currently no template does that).

## Problem 4: single-package npm wrapper variant

The npm wrapper codegen publishes per-platform packages via optionalDependencies, which requires claiming N extra registry names. Some projects decline extra name claims. Wanted: a single-package variant under the one existing name (single platform with `os`/`cpu` fields initially; multi-platform later = fat tarball with a runtime platform selector shim). Scaffold should make per-platform vs single-package an explicit mandatory choice.

## Affected files

`rlsbl/commands/init_cmd.py`, `rlsbl/checks/project.py`, `rlsbl/config.py`, `rlsbl/targets/__init__.py`, `rlsbl/targets/go.py`, `rlsbl/npm_wrapper.py`, `rlsbl/pipelines/npm.py`, `rlsbl/templates/npm/*.tpl`, new PyPI wrapper templates, docs.

## Effort estimate

Problem 1: small-medium (rename/migration or interactive init + docs). Problem 2: small. Problem 3: medium-large (the substantial new piece). Problem 4: small-medium. Independently landable; 1 and 2 first — they fix the misleading semantics the whole confusion stemmed from.
