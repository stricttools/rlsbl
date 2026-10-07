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
- The member layering system is dropped: layers.py, the `[layers]` workspace key, and the layers-violations check; the one workspace declaring `[layers]` has that section removed in the same change so its rlsbl commands keep working.
- `discover` is kept.
- `monorepo graph` is kept and gains `--json` output (members, versions, targets, and edges in topological order).
- `monorepo impact` and `monorepo outdated` are kept.
- The monorepo snapshot is dropped: the command, the release step, snapshot-check, and the committed snapshot.json files; `monorepo graph --json` gives the same inventory on demand.
- `claim-name` and `monorepo check-names` are kept.
- `prs` is dropped.
- The `pre-push-check` removal stub is deleted.
- `monorepo cleanup` is kept as the repair for releasable-residue findings (including the residue `monorepo absorb` leaves), not as a one-off migration.
- `release yank` and `release scrub` are kept.

## Lifecycle and license

- Lifecycle, license, and disclosure get first-class support through one record and one library, built in Go during the rlsbl rewrite: a strictspec-owned record per repository under `.strictmetadata/` holds dated periods of each releasable's lifecycle (active, on-hold, retired), license, and identities with the tag namespaces they owned, plus the repository's disclosure (confidential or public). One Go library evaluates a closed set of rule types fixed in code (rules while a value holds, rules over what was created during a period, permanent rules once triggered) and is linked by rlsbl, selfdoc, and safegit at their enforcement points. A dead identity's tags are accounted for by the identity's record, never scrubbed and never refused. Names of confidential subjects go into a machine-local index outside every repository, which safegit refuses at commit and selfdoc and rlsbl refuse in what they publish. The lifecycle, renamed-from, release-history-closed, and tag-ownership facts move out of rlsbl's transition record into this record, and the transition record goes back to recording repository surgery only. Stable subject IDs are not introduced.
- Proprietary means private: there is no separate disclosure value to declare. A releasable whose license is proprietary requires a private repository and refuses every public output (registries, public docs deploys, blog posts, attestations and provenance), and its names enter the confidential index. browserbuddy is therefore confidential: its public npm and PyPI publishing and its site listing stop.
- A project going from proprietary to public always has its proprietary-period history scrubbed with safegit first, and is then declassified (its repository made public); the transition command performs both in that order.
- Prose confidentiality is enforced by matching names plus each project's declared codenames and distinctive terms at commit; descriptions that avoid them stay covered by the written rule for sessions.
- Axiom: a registry name, once held, is never dropped, proprietary software included.
- Axiom: proprietary software may be split into a thin client and a server holding the proprietary logic; registries only ever receive the client, which protects the source. For such projects, releasing means publishing the client and deploying the server (the reason rlsbl had deploy). wavescript is the first planned case, after its own rewrite from Python to Go.
- The support matrix is kept.

## Private repositories, clients, and servers

- Private repositories are unknown to the public: their names appear nowhere public. A private project that has, or will have, a public client may be named.
- browserbuddy's published npm and PyPI versions are deprecated and yanked quietly, with a neutral notice that says nothing about it going private or proprietary; its registry names are kept.
- A server releasable declares a deploy command (for example `<infrastructure tool> deploy wavescript-server --version 0.4.0`, naming a private infrastructure tool only through the releasable's private config). After tagging, the release runs it and fails when it fails; `rlsbl release resume` retries the same version. Hosts, health checks, and rollback stay in the infrastructure tool, not in rlsbl.
- Licenses are declared per releasable, with dated periods in the lifecycle-and-license record; the private-repository and no-public-output rules apply per releasable, so a private repository may hold a releasable under a public license (a published client).
- The private-repository publishing guard (npm build provenance, PyPI attestations, Go module proxy notification) is kept and ported into the lifecycle-and-license library as a rule of private repositories: a release from a private repository refuses every publishing feature that records the repository's identity anywhere public.
- A Go client of a private repository is published only as built binaries, through npm per-platform packages and PyPI wheels; rlsbl refuses a Go library target on a private repository.
- Before publishing a client, rlsbl packs the artifact (npm pack, the wheel, the binaries) and refuses if any file comes from outside the client releasable's member paths, naming each file.

## The Go rewrite

- `private-hook-stale` is dropped.
- `requires-services` is dropped, with the `services` and `test_env` config keys; no config uses them.
- Every rlsbl config key is audited in the rewrite plan: declarations of what a project is move to `.strictmetadata/` subject directories, behavior switches become strictspec options or are deleted, and the Go rlsbl reads only the new places.
- The Go module goes in place at the repository root (`go.mod`, `cmd/rlsbl`, `internal/...`); the Python stays until the Go version passes, then is deleted in one commit, recoverable from history.
- On-disk record formats are redesigned freely, with a one-time migration command run and committed across every rlsbl repository (37 at the backfill dry run) before the switch; the Go rlsbl reads only the new formats.
- The Go rlsbl is distributed as a Go module and as prebuilt binaries through npm per-platform packages and PyPI wheels under the existing `rlsbl` names.
- The uncommitted version-agreement refusal another session left in `rlsbl/commands/init_cmd.py` (with its test `tests/test_scaffold_version_agreement.py`) is committed first, carried into the Go version, and deleted with the rest of the Python.
- The checks moving to strictcode are ported into strictcode in Go and strictcode is released, and the Go rlsbl's release checks call it, before the Python rlsbl is deleted.
- The rewrite is finished only when the Python is deleted and the Go rlsbl is released: the record migration is committed in every rlsbl repository (rlsbl-owned files only), strictcli, strictcode, selfdoc, safegit, saferm, and rlsbl are released as needed in dependency order, and the rlsbl guides under ~/Projects/CONTEXT are rewritten for the Go commands.
- The last Python version is 0.131.0.

## Orchestrator rulings on the rewrite plan

Made by the session leading the campaign, not by the owner.

- `library-lint`, `dead-workspace-packages`, and `ruff-lint` move to strictcode; `ruff-lint` merges into strictcode's lint rule. The Go rlsbl parses no source.
- The `rlsbl:strictcode` option's value is `error`: every release runs strictcode with all its rules.
- The cgofree tree-sitter modules are released before strictcode, as subdirectory modules inside the cgofree repository named after their existing directories and tagged `<dir>/vX.Y.Z` there; no new repositories and no new registry names. cgofree joins the release order before strictcode. No 1.x tag.
- A repository with no lifecycle-and-license record is treated as public, and confidential names are refused there. The migration writes a record for every rlsbl repository, and a command writes a minimal record for repositories rlsbl does not manage.
- `transition declassify` squashes each proprietary period's commits into one commit whose message names no confidential term, keeping the public history before and after it.
- rlsbl's npm per-platform packages are `rlsbl-linux-x64`, `rlsbl-linux-arm64`, `rlsbl-darwin-x64`, and `rlsbl-darwin-arm64`; there are no win32 packages.
- Preflight checks always run; a declared pre-release hook replaces only the built-in tests (option A of `todo/preflight-checks-skipped-by-custom-hooks.md`, which moves to `todo/.done/` with the resolution when built).
- The record has no `disclosure` field: a repository is confidential when one of its releasables currently has a proprietary license period, and safegit derives that from the license periods offline. Private repositories without a proprietary releasable are not designed for.
- The migration refuses a config naming a removed target and names the hand edit.
- strictspec, which holds the lifecycle-and-license library, joins the release order; saferm is the first project released with npm per-platform packages, before rlsbl.
- `release undo --version` evidence and the registry probe in `rewrite uv-path-sources` use package-level listings of already-published versions, with tests; no request ever names one of our unpublished versions.
- A releasable whose lifecycle is on-hold refuses releases.
