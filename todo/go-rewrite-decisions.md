# Rewriting rlsbl in Go: decisions

Append-only record of the owner's rulings for slimming rlsbl and rewriting it in Go. A later ruling overrides an earlier one.

## Slimming before the rewrite

- rlsbl keeps three release targets: go, npm, and pypi. Every other target is removed: dart, deno, docker, flutter, hex, maven, native_android, native_ios, native_changes, pgdesign, plain, swift, swift_apple, and zig, with every registry entry, template, check, option, and test that exists only for them.
- The slimming happens in the Python first, by deletion, and the Go rewrite then ports the smaller tree. The removed code stays recoverable from git history.
- The two conformance release configs, stricttools/tools/strictcli/conformance/.rlsbl/config.json and stricttools/tools/strictspec/conformance/.rlsbl/config.json, are deleted with the plain target; the conformance suites are deleted under the Great Refinement rule anyway.
- Projects whose configs name a removed target (gamehome's zig and pgdesign targets, safegit's docker target) are left as they are; they are fixed when they are next released.
- A read-only survey maps rlsbl's remaining subsystems (what each does, who uses it, its size), and the owner rules keep or drop on each before the rewrite plan is written.

## Implementation decisions while slimming

Recorded by the session that deleted the removed targets; each is an implementation choice, not an owner ruling.

- The spec target is kept. The ruling keeps three targets, yet its list of removed targets does not name spec, so spec was left registered for the owner to rule on; every supported-target list rlsbl prints is derived from the registry and names spec while it is registered.
- A configured target name rlsbl does not register is now a hard error naming the supported targets, in a project's `.rlsbl/config.json` and in a releasable's config alike. It replaces the warning that skipped the name, and it is the whole handling of configs naming a removed target: there is no migration path.
- The Flutter release mode is removed with flutter: the release file's `[targets.<name>]` table and its `mode` field, the changelog line's `release_type` field, the `last_build_release` config key, and the over-the-air validation. The strictspec schemas for the release file and the changelog line were narrowed and their validators regenerated with strictspec 0.5.0, so a document carrying either field is refused as an unknown key.
- Dart and JVM source analysis is removed with dart, flutter, and maven: the import scanners, dead-module and cycle detection, the workspace graph's pubspec and Gradle scanners, the Gradle unrecognized-dependency refusal, and the `depends_on` acknowledgment that existed only to clear it.
- The `maven-central-metadata` and `mirror-required` checks are removed, with their options. `mirror-required` and the `consumed_by_repository_url` axis served only swift and swift-apple; the subtree mirror machinery itself is kept.
- The deno, hex, maven, maven-central, and docker pipeline types are removed, with `CredentialPipeline`, which only docker used, and the config schema's pipeline type list is narrowed to match.
- Removed with the targets that alone used them: the Gradle lockfile re-lock and its wrapper refusal, the maven test runner, the subprocess linter path and `lint_library`'s `check_timeout` parameter, the deno scratch and nested-member exclusions, the `.dockerignore` helpers, the Docker metadata-action exemption in `scaffold-unreplaced-vars`, the deno, mix, and pubspec lockfiles among the router's root triggers, the action pins for the removed ecosystems, and the observe allowlist entry for `go env GOVERSION`.
- Kept for the subsystem survey rather than removed: target axes whose remaining answers are now uniform (`auto_detectable`, `ensure_ci_inputs`), and the `--target` flag of `rlsbl scaffold`, whose case for a target auto-detection cannot find no longer arises with the kept targets.
- Not edited, and left for a follow-up: `rlsbl/commands/init_cmd.py`, which held another session's uncommitted work (its `USER_OWNED` set still lists `.dockerignore`, and comments name zig, plain, and Docker); the module docstring of `rlsbl/private_paths.py`, which still mentions the Docker build context, because the module's source is embedded verbatim in every scaffolded pypi CI workflow, rlsbl's own included, so editing it means re-scaffolding those workflows; `rlsbl/targets/AXES-WORKLIST.md`, a record of the axes migration that names the removed targets; and the names of test files that mention removed targets (`test_batch_release_init_flutter.py`, `test_monorepo_add_plain.py`, `test_scaffold_plain_devnode.py`), which await a naming decision.
- `library-lint` stays declared impure, though the maven linter was the only reason it started a program that writes.
- Tests that used a removed target as a convenient fixture were moved to a kept target, mostly spec, the kept target that only bumps a version.

## Rulings on the subsystem survey

- `monorepo extract` and `monorepo absorb` are kept.
- The subtree mirror is dropped: the `monorepo mirror` command, mirror publication, the release mirror step, and its checks.
- Deploy is dropped: deploy.py, its config key, and its release step.
- Fork-upstream support is kept: `upstream adopt-tags` and the upstream filters in changelog coverage.
- The prerelease channel is dropped: the preid option and its branches in version computation, tagging, and publishing.
- Blog-on-release is dropped: the release file field and its release steps.
- npm also ships Go binaries, the way esbuild does: one package per platform holding the binary, selected through `optionalDependencies` with `os` and `cpu`, and no postinstall script. rlsbl's npm wrapper feature is kept and ported for this; it replaces the hand-written postinstall downloaders (saferm's and pgdesign's) when those projects next release.
- The launcher artifact is dropped.
- The `spec` target is dropped, so the supported targets are exactly go, npm, and pypi.
- Local asset upload (`assets`, `custom_assets`) is dropped: the generated publish workflow already builds and attaches release archives in CI.
- The Cloudflare Pages pipeline is dropped: selfdoc deploys documentation sites itself.
- The lint, format, and type-check tool options, the dependency-declaration checks (deps-*), and the dead-modules check move out of rlsbl to strictcode.
- The checks moving to strictcode are ported into strictcode in Go during this campaign (with their tests and the recent false-positive fixes), then deleted from rlsbl.
- The strictspec certificate check moves to strictcode as well.
- The circular-deps check moves to strictcode as well.
