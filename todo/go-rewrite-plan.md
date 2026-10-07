# Rewrite rlsbl from Python to Go

rlsbl is rewritten in place to plain Go. The rewrite keeps what rlsbl is for (versioning, a structured changelog, releases that tag only the commit CI verified, publishing to npm, PyPI, and the Go module proxy, scaffolding, workspaces with releasables) and carries out every ruling in `todo/go-rewrite-decisions.md`. It is not a line-for-line port: the subsystems the rulings drop are not ported, every record format is redesigned, every declaration moves under `.strictmetadata/`, and the lifecycle-and-license record is built beside it.

Inputs, in order of authority:

- `todo/go-rewrite-decisions.md`: the owner's rulings. A later ruling overrides an earlier one. This plan carries out every ruling; where this plan and the decisions file disagree, the decisions file wins and the disagreement is a deviation.
- `experiments/survey/subsystems.md` and its companion files: the survey the rulings were made on (gitignored scratch, so it is evidence for this plan only).
- The Python tree `rlsbl/` and `tests/` at the commit this plan is committed on: the behavior to port, minus what the rulings drop.
- `~/Projects/CONTEXT/strict.md` (the `.strictmetadata/` layout and the options rule) and `~/Projects/CONTEXT/building-a-cli.md` (strictcli's effects regime, consent, and flag naming).

Deviations found during the work are appended to `todo/go-rewrite-plan.deviations.md`, one entry per deviation: what the plan says, what the work found, what was built instead. The plan file itself is not edited after it is committed.

## Execution rules for every implementor

- The owner has forbidden running anything heavy during the build: no builds, no compiles, no `go vet`, no test suites, no package installs, no code generators that compile, no migrations across repositories, and no releases. Reading, `git log`/`status`/`diff`, `grep`, `ls`, `wc`, small read-only scripts, `safegit commit`, and `saferm delete` are allowed. Every step in this plan that needs a forbidden action is marked **coordinator step**: the implementor stops there and returns to the coordinator, who runs it alone, one heavy process at a time.
- Tests are written as files beside the code in every slice and are compiled and run for the first time in the first-run phase.
- Code is formatted by hand the way `gofmt` would format it (tabs, sorted import groups); the first-run phase runs `gofmt -l` and fixes what it lists.
- Commit as you go with `safegit commit -m "..." -- <files>`, one commit per coherent item. Never `git add`, `git commit`, `git stash`, or push.
- The Python tree stays untouched until the switchover commit, except for the bug fixes the owner orders separately.
- Never name a private repository in any file of this repository. Fixtures use invented names (`portal`, `widget`, `gadget`).
- No Go identifier, comment, file name, data field, or error text uses the word "kind"; use "type", "class", or a word that names the thing. No identifier, test name, comment, or string names a slice, phase, or step of this plan.
- Every error that names a fix gets a red-green test that performs the fix and asserts the error clears.
- A bug found in the Python behavior being ported gets a red test first, then the fix, in the slice that ports it, and an entry in the deviations file.
- An `os/exec`, `net/http` client call, or file write outside a strictcli effects handle is a defect; strictcli's `effects-bypass` check enforces it once the suite runs.

## Module and layout

- One Go module at the repository root: module path `github.com/stricttools/rlsbl`, `go 1.26.3` (strictspec/go declares 1.26.3, and a main module may not declare less than a dependency).
- One binary, `cmd/rlsbl`. `cmd/rlsbl/main.go` builds the app through `internal/cli` and runs it; nothing else lives there.
- A committed `go.work` uses `.`, `../strictcli/go`, and `../strictspec/go` while the build needs unreleased additions there. Before rlsbl's first Go release the `require` lines are raised to the released versions and those two `use` lines are removed (see "After the build").
- Embedded data (`checks.toml`, the options registry, templates, action versions, the Go standard-library package table, the support matrix) is embedded with `//go:embed` from the package that owns it.
- Generated Go files carry a `// Code generated ... DO NOT EDIT.` header and each has a freshness test that re-renders it and compares.

### Packages

| Package | Responsibility | Python it replaces |
| --- | --- | --- |
| `cmd/rlsbl` | `main` only. | `rlsbl/__main__.py` |
| `internal/cli` | The strictcli app: every command and group registration (one file per group), flag and argument declarations, payload types for `--json`, human renderers, the check value resolver wiring, and the embedded checks registry. | `rlsbl/__init__.py`, `context.py`, `check_context.py`, the thin command wrappers under `commands/` |
| `internal/testsupport` | Test-only helpers: git fixture repositories and `file://` remotes, the fake `gh` (the test binary re-executed under the name `gh`), the fake HTTP transport, declaration builders, and the isolation guard test. Imported only from `_test.go` files. | `tests/conftest.py` and the shared fixtures |
| `internal/semver` | Version parsing, comparison, and bumps (`patch`, `minor`, `major`, `infra`). No pre-release channel. | the version parts of `utils.py` |
| `internal/git` | Git plumbing through the effects handle: rev-parse, ancestry, refs, ls-remote, trees, commits in a range, trailers, push with an explicit lease, merge-file, stash and clean-tree detection, the post-rewrite stdin parser. | `git_util.py`, the git parts of `utils.py` |
| `internal/github` | `gh` through the effects handle: auth check, repository identity and visibility, Releases (create, edit in place, view, pre-release flag, the `rlsbl-ci-sha` marker), topics, Actions secrets (presence, set), workflow runs and dispatch, repository search. | the `gh` parts of `utils.py`, `ci_secrets.py` presence reads, the API parts of `tagging.py` and `commands/discover.py` |
| `internal/saferm` | The `saferm delete --on-error abort --description ...` argv builder; a missing saferm is a hard error. | `saferm.py` |
| `internal/declarations` | Strict reading and comment-preserving writing of `.strictmetadata/releasables/releasables.toml` and `.strictmetadata/test-runner/test-runner.toml`; the strictspec schemas for both and their generated validators. | `config.py`, `workspace.py` (load and save), `workspace_types.py`, `member_context.py`, `resolved_target.py`, `strictspec_gen/config_validator.py` |
| `internal/workspace` | The in-memory model built from the declarations: releasables, members, the root member, nested members, file ownership and territory, tag formats and tag globs, the dependency graph and topological order, intra-workspace constraints, dev nodes. | `ownership.py`, `tag_glob.py`, `workspace_graph.py`, `constraints.py` |
| `internal/options` | rlsbl's options registry (generated from the checks registry plus the options that are not checks), loading `.strictmetadata/options/` through strictspec/go, the check value resolver, and the `options set` writer. | `options.py`, `options_registry.py`, `data/options.toml`, `scripts/gen_options_registry.py` |
| `internal/changelog` | Entry schema and generated validator, changelog files, validation (hashes, range, coverage, orphans, schema, user-facing, batch limits, format version), exemptions (the `Autogenerated: true` trailer, changelog-only commits), coverage enumeration, `CHANGELOG.md` generation and the workspace roll-up, hash remapping, the validation cache. | `changelog/*.py`, `prepush_utils.py`, the logic of `commands/changelog_cmd.py`, `strictspec_gen/changelog_entry_commit_validator.py` |
| `internal/releaserecord` | Release files (single and batch), release archives with their three fates, the nearest and latest release, the record's read errors, the transition record (surgery events only), tag explanation, release-commit remapping. | `release_file.py`, `release_record.py`, `transition_record.py`, `transition_record_followup.py`, `tag_explanation.py`, `release_commit_remap.py`, `strictspec_gen/release_file_validator.py`, `strictspec_gen/transition_record_event_validator.py` |
| `internal/targets` | The target protocol for go, npm, and pypi: detection, version read and write (`VERSION`, `package.json`, `pyproject.toml` and `__version__`), metadata (name, license, description), test runner selection, build and clean, upload listing (upload exclusions, nested-member exclusions, private paths), and the support matrix generator. | `targets/*.py` (minus `spec.py`), `testing.py`, `upload_exclusions.py`, `nested_exclusions.py`, `private_paths.py`, `data/support-matrix.json`, `scripts/generate_support_matrix.py` |
| `internal/pipelines` | Publish pipeline semantics: the `npm`, `pypi`, and `go` pipeline types, artifact values, CI secret names, local installs, and the per-platform table shared by npm platform packages and PyPI binary wheels. | `pipelines/{base,go,npm,pypi,protocol,introspect}.py`, `npm_wrapper.py` |
| `internal/gomodule` | `go.mod` parsing (`golang.org/x/mod/modfile`), module containment, module identity against origin, the toolchain line, `go list` introspection, `go.work` checks, `-X` ldflags symbol validation with `go/parser`. | `go_introspect.py`, `module_paths.py`, `go_identity.py`, `go_toolchain.py`, `go_mod.py`, `ldflags_symbols.py` |
| `internal/dependencies` | `pyproject.toml` and `uv.lock` reading, the uv workspace locator, path-dependency rewriting for the PyPI build, dependency version floors, lockfile freshness (uv, npm, Go), the strictspec generated-format floor. | `uv_workspace.py`, `dep_rewrite.py`, `dep_floors.py`, `dep_locks.py`, `strictspec_floor.py` |
| `internal/registry` | Package-level registry listings (npm, PyPI, Go proxy `@v/list`), name availability and placeholder claims, the offline Go package-name judgment with its standard-library table. | `registry.py`, `package_names.py`, `go_package_name.py`, `data/go-stdlib-packages.json`, `scripts/gen_go_stdlib_table.py`, `commands/check.py`, `commands/claim_name.py` |
| `internal/checks` | Every kept check, by family, plus the `strictcode` check and config-declared external checks, registered through strictcli's check framework from the embedded `checks.toml`. | `checks/*.py` (minus `strictspec_gate.py`), `external_checks.py`, the check half of `test_sandbox.py`, `data/checks.toml` |
| `internal/scaffold` | Templates (embedded), the template renderer, plans and three-way merges against stored bases, the scaffold state, git hook installation, action version pins, CI YAML assembly, the npm Node matrix, scratch directories, the test runner rendering, and the version-agreement refusal. | `commands/init_cmd.py`, `templates/`, `hook_hashes.py`, `action_versions.py`, `data/action_versions.toml`, `ci_yaml.py`, `node_matrix.py`, `scratch_dirs.py`, the rendering half of `test_sandbox.py` |
| `internal/workflows` | Workspace CI generation: the inlined `ci-router.yml` and `publish.yml`, router path filters, the publish job that waits for CI, and the platform-package and wheel jobs for Go binaries. | `commands/monorepo/sync.py`, `commands/monorepo/publish_inline.py`, `ci_router.py`, `router_filters.py`, `publish_gate.py` |
| `internal/ci` | Observing CI: run discovery and verdicts (green, red, timeout, not configured), whether publish workflows started, `watch` with classified retry, workflow dispatch for `release retry`. | `ci_checks.py`, `publish_workflows.py`, `commands/watch.py`, the dispatch half of `commands/release_retry.py` |
| `internal/publishrules` | rlsbl's side of the lifecycle-and-license library at release time: repository visibility from GitHub, the private-repository publishing refusals, the packed-artifact contents check, and the confidential-name scan of everything rlsbl publishes. | `private_repo_publishing.py` (its rule moves into the library) |
| `internal/release` | The single-releasable release: validation, the release checkout, the advisory lock, run state, the pre-mutation plan, hooks, version writes and lockfile sync, build, secret scan, the steps from commit to post-release, deploy, resume, and `release init`; also `status`, `unreleased`, `targets`, and `commit`. | `commands/release/*.py`, `release_checkout.py`, `lock.py`, `secret_scan.py`, `strictcli_detect.py`, `commands/release_init.py`, `commands/status.py`, `commands/unreleased.py`, `commands/targets_cmd.py`, `commands/commit_cmd.py` |
| `internal/releasenotes` | Composition of GitHub Release bodies from the changelog, the archive, and its notices, with the `rlsbl-ci-sha` marker; used by the release, by the commands on past releases, and by the history rewrites. | `release_publication.py` |
| `internal/releaseops` | Commands on past releases: `edit`, `retry`, `undo` with its evidence, `abandon`, `deprecate`, and `yank`. | `commands/edit_release.py`, `commands/release_retry.py`, `commands/undo.py`, `evidence_gate.py`, `publication_probe.py`, `commands/release_abandon.py`, `commands/deprecate.py`, `commands/yank.py` |
| `internal/historyrewrite` | `release scrub`, `release reconcile`, `release backfill`, and the observe-preview-apply skeleton they share. | `commands/release_scrub.py`, `commands/release_reconcile.py`, `release_backfill.py`, `commands/release_backfill.py`, `preview_apply.py`, `strictspec_gen/reconcile_plan_validator.py` |
| `internal/monorepo` | `monorepo init`, `add`, `remove`, `list`, `sync` (command half), `status`, `check-names`, `outdated`, `graph`, `impact`, `cleanup`, `rename-releasable`, `extract`, and `absorb`. | `commands/monorepo/{commands,graph,impact,extract,extract_cmd,absorb_cmd,releasable_rename}.py`, `releasable_cleanup.py` |
| `internal/batchrelease` | `monorepo release run`, `init`, and `order`, and the batch plan. | `commands/monorepo/{batch_release,batch_release_init,batch_plan}.py` |
| `internal/devtools` | `dev install`, `dev sync`, `dev status`, and the overlay state. | `commands/dev.py`, `commands/dev_sync.py`, `overlay_state.py` |
| `internal/rewrite` | `rewrite go-module-path` (with `go/parser` instead of tree-sitter), `rewrite project-name`, `rewrite uv-path-sources`. | `commands/rewrite/*.py` |
| `internal/secrets` | `secrets sync-npm-token`, npm token validity, and the token-synced comparison. | `npm_token.py`, the write half of `ci_secrets.py` |
| `internal/tagging` | The rlsbl keyword in `package.json` and `pyproject.toml`, the rlsbl GitHub topic, and `discover`. | `tagging.py`, `commands/discover.py` |
| `internal/upstream` | `upstream adopt-tags` and the coverage filters for forks. | `upstream.py`, `commands/upstream_cmd.py` |
| `internal/lifecycleops` | The `transition` command group: the commands that write the lifecycle-and-license record, `transition classify`, and `transition declassify`. | `commands/transition_record_cmd.py` (replaced) |
| `internal/migration` | The one-time record migration command. Deleted in the switchover commit. | new |

A layering test (`internal/cli/layering_test.go`) parses every package's imports and refuses an import that points up this order, lowest first: `semver`, `git`, `github`, `saferm`; then `declarations`; then `workspace`, `options`, `upstream`; then `releaserecord`, `gomodule`, `dependencies`, `registry`; then `changelog`, `targets`; then `pipelines`; then `publishrules`, `releasenotes`, `devtools`, `secrets`, `tagging`, `workflows`, `scaffold`; then `checks`, `ci`; then `release`; then `releaseops`, `historyrewrite`, `batchrelease`, `monorepo`, `rewrite`; then `lifecycleops`, `migration`; then `cli`. An import between two packages of one group is allowed only where the test's allow table names the pair, and the slice that adds such an import adds the pair with a one-line reason.

### Dependencies

- `github.com/stricttools/strictcli/go` for the app, the effects handle, and the check framework (`WithChecksEmbed`, `RegisterCheckProvider`, `SetCheckValueResolver`, `RunChecks`).
- `github.com/stricttools/strictspec/go` for the generated validators' runtime, the options readers (`LoadOptionsEntries`, `ReadOptionsRegistry`, `LoadUpstream`), and the lifecycle-and-license package.
- `github.com/pelletier/go-toml/v2` for strict decoding (`DisallowUnknownFields`) and `github.com/stricttools/go-toml-edit` v0.5.0 for in-place edits that keep comments.
- `golang.org/x/mod` (`modfile`, `module`, `semver`, `zip`) for Go modules.
- `gopkg.in/yaml.v3` for reading GitHub workflow files (generation is by templates, never by YAML serialization).
- `github.com/stricttools/testisolation/go` (`hygiene`) in every test.
- Everything else is the standard library. There is no tree-sitter or other parser dependency: rlsbl analyzes no source (every source analysis moved to strictcode). The standard library's `go/parser` is used only to locate Go import sites for `rewrite go-module-path`, to validate `-X` ldflags symbols, and in the guard tests.

## Command surface

The surface is the Python surface (`.strictmetadata/.cli-schema/schema.json` at the planning commit) minus the dropped commands, with the changes listed after the table. The reserved quartet (`--dry-run`, `--approve-consequential`, `--quiet`, `--verbose`) is on every command, and strictcli's framework `--json` is on every command that declares a payload; neither is repeated. "C" marks a consequential command. "no dry run" means `WithDryRunUnsupported` with the reason the Python gives.

| Command | Arguments and flags | Effect | Notes |
| --- | --- | --- | --- |
| `check` | `--all`, `--tag`, `--name`, `--hook`, `--list` | read_only | strictcli framework command. |
| `failing-checks` | as `check` | read_only | The pre-push hook runs `rlsbl failing-checks --hook pre-push`. |
| `status` | `--target`, `--registry` | read_only | `--registry` uses package-level listings only (see "Registry lookups"). |
| `unreleased` | none | read_only | |
| `targets` | none | read_only | |
| `commit` | `--message`, file arguments | mutating, no dry run | Commits with the `Autogenerated: true` trailer. |
| `scaffold` | `--target`, `--publish-mode`, `--auto-commit`, `--skip-shared` | mutating | `--auto-tag` is removed: the `rlsbl:ecosystem-tagging` option decides. |
| `check-name` | names, `--target` (repeatable: npm, pypi, go), `--delay` | read_only | |
| `claim-name` | name, `--target` | mutating, C | |
| `discover` | `--mine` | read_only | |
| `watch` | `[sha]`, `--target`, `--run-id` | mutating | Reruns a classified-transient failure once. |
| `release run` | `--watch`/`--no-watch` (required), `--push-timeout`, `--ci-timeout`, `--check-timeout`, `--hook-timeout`, `--releasable` | mutating, C | `--releasable` is required where the working directory does not select one releasable, and refused where it does. |
| `release resume` | as `release run` without `--releasable` | mutating, C | |
| `release init` | none | mutating, no dry run | |
| `release retry` | `--watch`/`--no-watch` | mutating, C | |
| `release edit` | `[version]` | mutating | |
| `release undo` | `--target`, `--version` | mutating, C | Evidence never asks a registry about one version (see "Registry lookups"). |
| `release abandon` | none | mutating, C | |
| `release deprecate` | `version`, `--reason`, `--use` | mutating, C | |
| `release yank` | `version`, `--reason`, `--use` | mutating, C | |
| `release scrub` | `--mode pattern\|file\|recipe` (with `--pattern`, `--replace`, `--mangle`, `--file`, `--recipe` in their scopes), `--commit-range from-commit\|entire-history` (with `--from-commit`), `--reason` | mutating, C | Requires safegit at the version this campaign releases (its `--json` interface version is checked). |
| `release backfill` | `--overrides`, `--auto-commit` | mutating, C | |
| `release reconcile` | `--mode plan\|apply`, `--push-timeout`, `--releasable` | mutating, C | |
| `changelog add` | `--commits`, `--description`, `--type`, `--user-facing`, `--auto-commit`, `--batch-reason` | mutating | `--allow-batch` becomes `--batch-reason <text>`, which writes the reason onto the entry. |
| `changelog generate` | `--auto-commit` | mutating | |
| `changelog amend` | `--version`, `--commits`, `--id`, `--description`, `--type`, `--user-facing`, `--validate-hashes` | mutating | |
| `changelog edit` | `--commits`/`--id`, `--type`, `--description`, `--user-facing`, `--unset-description`, `--unset-type`, `--auto-commit` | mutating | Sparse update, as in Python. |
| `changelog remove` | `--entry id\|commits` (with `--id`/`--commits`), `--auto-commit` | mutating | |
| `changelog remap` | `--map-file`, `--from-journal`, `--stdin` | mutating | The post-rewrite hook runs `rlsbl changelog remap --stdin`. |
| `monorepo init` | `--root-member root-dev-node\|root-releasable` (with `--releasable`, `--tag-format`, `--publish-mode`, `--publish-ci-check-pattern`), `--auto-commit` | mutating, no dry run | `--publish-gate-check-regex` is renamed `--publish-ci-check-pattern`. |
| `monorepo add` | `path`, `--name`, `--target`, `--depends-on`, `--library`, `--dev-only`, `--releasable`, `--tag-format`, `--registry-name`, `--auto-commit` | mutating | |
| `monorepo remove` | `path` | mutating, no dry run | |
| `monorepo list` | none | read_only | |
| `monorepo sync` | `--auto-commit` | mutating | |
| `monorepo status` | none | read_only | |
| `monorepo check-names` | `--target`, `--prefix`, `--suffix`, `--delay` | read_only | |
| `monorepo outdated` | none | read_only | |
| `monorepo graph` | `--format dot\|tree`, `--output`, `--root`, `--reverse`, `--depth` | mutating | `--json` prints members, versions, targets, and edges in topological order (ruling). |
| `monorepo impact` | `--depth`, `--since`, package or file arguments | read_only | |
| `monorepo extract` | `releasable_name`, `target_path`, `--delete-with-rm` | mutating, C | The filter engine only; the mirror promotion engine is dropped with the mirror. |
| `monorepo absorb` | `source_repo`, `dest_path`, `--name`, `--registry-name`, `--releasable`, `--tag-format`, `--delete-with-rm` | mutating, C | |
| `monorepo cleanup` | `--auto-commit` | mutating | Removes old-layout residue (see "monorepo cleanup"). |
| `monorepo rename-releasable` | `old_name`, `new_name` | mutating, C | Writes identity periods into the lifecycle-and-license record. |
| `monorepo release run` | as `release run` without `--releasable` | mutating, C | |
| `monorepo release init` | `--releasables` | mutating, no dry run | |
| `monorepo release order` | none | read_only | |
| `dev install` | `--target global\|venv`, `--all`, `--include`, `--exclude`, `--uninstall` | mutating | |
| `dev sync` | none | mutating | |
| `dev status` | none | read_only | |
| `rewrite go-module-path` | `--from-module`, `--to-module` | mutating | |
| `rewrite project-name` | `--from`, `--to` | mutating, C | Writes an identity period instead of a transition event. |
| `rewrite uv-path-sources` | none | mutating | |
| `transition show` | none | read_only | New: prints the lifecycle-and-license record and every rule's current verdict. |
| `transition lifecycle` | `--subject`, `--status active\|on-hold\|retired`, `--reason` | mutating, C | New. |
| `transition license` | `--subject`, `--license`, `--reason` | mutating, C | New. Refuses `proprietary` (use `transition classify`) and refuses a change that would leave no releasable with a current `proprietary` license (use `transition declassify`). |
| `transition identity` | `--subject`, `--facet`, `--value`, `--registry`, `--tag-pattern` (repeatable), `--from`, `--until`, `--reason` | mutating, C | New: declares an identity period, including a dead identity's. |
| `transition unversioned-tag` | `--tag`, `--reason` | mutating, C | New: replaces `transition record --fact non-version-tag`. |
| `transition classify` | `--subject`, `--reason` | mutating, C | New: makes a releasable proprietary (see "The lifecycle-and-license record"). |
| `transition declassify` | `--license <releasable>=<SPDX identifier>` (repeatable, one per releasable whose current license is `proprietary`), `--reason` | mutating, C | New: the going-public command the ruling names (squash each proprietary period, then make the repository public). |
| `transition init-minimal-record` | none | mutating | New: writes a minimal lifecycle-and-license record in a repository rlsbl does not manage (see "The lifecycle-and-license record"). |
| `upstream adopt-tags` | none | mutating, C | |
| `secrets sync-npm-token` | `--all` | mutating, C | |
| `options registry` | none | read_only | |
| `options set` | `id`, `--current`, `--ideal`, `--reason`, `--scope`, `--auto-commit` | mutating | |
| `migrate records` | none | mutating, C | One-time; exists only until the switchover commit (see "The record migration"). |

Every command's help text is rewritten for the Go behavior in the help-text slice; flag spellings above are binding.

### Dropped commands

| Command | Ruling |
| --- | --- |
| `deploy` | "Deploy is dropped: deploy.py, its config key, and its release step." |
| `prs` | "`prs` is dropped." |
| `pre-push-check` | "The `pre-push-check` removal stub is deleted." |
| `monorepo snapshot`, `monorepo snapshot-check` | "The monorepo snapshot is dropped". |
| `monorepo mirror` | "The subtree mirror is dropped". |
| `transition record` | Replaced by the `transition` subcommands that write the lifecycle-and-license record, because its three facts move into that record (ruling on lifecycle and license). |

## Checks

The checks registry moves to `internal/checks/checks.toml` (embedded). Each kept check keeps its tags, severity, `depends_on`, and option. Renamed checks drop a word the house style bans; nothing outside rlsbl's own options entries names them.

Kept, unchanged in name: `lock`, `version-consistency`, `name-consistency`, `license-consistency` (now also compares every manifest's license with the record's current license), `description-consistency`, `license-file`, `unpublished-refs`, `branch-sync`, `ci-publish-secrets`, `npm-token-synced`, `private-repo-publishing`, `old-repo-archived`, `go-deprecation-published`, `changelog-entry`, `changelog-hashes`, `changelog-range`, `changelog-coverage`, `changelog-orphans`, `changelog-schema`, `changelog-user-facing`, `changelog-batch-commits`, `changelog-batch-entries`, `router-filters-fresh`, `workspace-ci-router`, `workspace-ci-synced`, `workspace-targets`, `workspace-unregistered`, `workspace-stale-entries`, `dev-only-boundary`, `unversioned-boundary`, `workspace-unbuildable`, `scaffold-unreplaced-vars`, `publish-mode-workflow`, `npm-private-mismatch`, `target-version-readable`, `dunder-version-missing`, `selfdoc-version-drift`, `scaffold-conflicts`, `stash-free`, `cross-repo-path-sources`, `target-matrix-fresh`, `dev-overlay-drift`, `prepush-changelog-coverage`, `prepush-gitignore-guard`, `prepush-manual-warning`, `test-suite`, `test-suite-workspace`, `scaffold-gitignore-stale`, `go-companion-tags`, `releasable-residue`, `member-pytest-config`, `mixed-tag-schemes`, `testisolation-floor`, `dep-floors`, `strictspec-generated-format`, `go-module-identity`, `path-tag-format-go-member`, `go-module-major-suffix`, `nested-member-runner-exclusion`, `nested-member-upload-contents`, `upload-private-paths`, `nested-member-uv-sources`, `go-workspace-require-current`, `go-workspace-replace`, `go-toolchain-declared`, `ldflags-symbol`, `dep-locks`.

Renamed: `changelog-format-version-gate` becomes `changelog-format-version`; `config-schema` becomes `declarations-valid` (the declarations it validates are no longer a config file).

Added:

| Check | Tags | Severity | What it verifies |
| --- | --- | --- | --- |
| `strictcode` | `quality`, `preflight` | error | `strictcode analyze <repository root> --json` reports no error finding (see "How rlsbl calls strictcode"). Its option's default is `error`: every release runs strictcode with all its rules. |
| `lifecycle-record-valid` | `project`, `preflight` | error | The lifecycle-and-license record exists, passes its schema and every rule the library checks on the record alone, and keeps every closed period and every held registry name present in the record at the nearest release commit (the append-only rule). |
| `confidential-names` | `project`, `preflight` | error | In a public repository, no tracked file and no changelog entry contains a name from the machine-local confidential index. |
| `repository-visibility` | `release` | error | Whether the repository is confidential (see "The record") agrees with GitHub's visibility of the repository (confidential with private, public with public); a private repository with no proprietary releasable is refused, naming `transition classify` and making the repository public as the two ways out. The release performs the same comparison as a direct validation, so the network condition is part of release validation and not of preflight. |

Dropped, with the ruling: `private-hook-stale` and `requires-services` (rulings on the Go rewrite); `layers-violations` (member layering dropped); `subtree-remote-reachable` (mirror dropped); `wrapper-producer` (launcher dropped); `lint`, `lint-scope-guard`, `format`, `format-scope-guard`, `type-check`, `type-check-scope-guard`, `deps-unused`, `deps-undeclared`, `deps-stale`, `deps-runtime-test-only`, `deps-dev-in-lib`, `dead-modules`, `dead-modules-stale`, `circular-deps`, `strictspec-certificate-gate`, `library-lint`, `dead-workspace-packages`, `ruff-lint` (moved to strictcode; `ruff-lint` merges into strictcode's `lint` rule); `root-rlsbl-conflict` (its case, an old-layout `.rlsbl/` at a workspace root, is reported by `releasable-residue` after the migration).

`checks.toml` keeps every Python field (`description`, `subject`, `tags`, `severity`, `fast`, `pure`, `needs_network`, `depends_on`, `scope`) and its machine-input declaration; preflight refuses a check that declares a machine input, as in Python, with `npm-token-synced` the one check that declares `~/.npmrc`, as in Python.

## Options

- Every kept check is an option `rlsbl:<check name>` ranked `error > warn > off` (or `warn > off`), default its severity, as the Python registry derives it, generated into `internal/options/registry.toml` by `internal/options/gen` with a freshness test.
- Options that are not checks: `rlsbl:test-sandbox` (`on > off`, default `off`, path scope) and the new `rlsbl:ecosystem-tagging` (`on > off`, default `on`) replacing the `tag` config key, the user-level config file, and scaffold's `--auto-tag`.
- `rlsbl:dep-floors` stays; its setting moves into the declarations.
- Removed with their checks or settings: `rlsbl:format`, `rlsbl:lint`, `rlsbl:type-check`, `rlsbl:strictspec-certificate-gate`, and every option of a dropped check are removed. The migration rewrites any entry naming a removed or renamed option and refuses, naming it, an entry that has no new home.
- strictcli's framework checks (`cli-test-coverage`, `consequential-grant-agreement`, `effects-bypass`, `observe-allowlist-breadth`) stay options as in Python.

## The new on-disk layout

Every file rlsbl owns in a repository moves under `.strictmetadata/` (`~/Projects/CONTEXT/strict.md`: a family tool's directories live there, are named for what they hold, never after the tool, and start with a dot if and only if they are generated). The Go rlsbl reads only these places. `CHANGELOG.md` at the repository root and generated files under `.github/workflows/` stay where they are because readers outside rlsbl look there.

| Path | Authored or generated | Owner (`manifest.toml`) | Holds |
| --- | --- | --- | --- |
| `.strictmetadata/releasables/releasables.toml` | authored | rlsbl | The repository's release declarations: releasables, members, targets, pipelines, hooks, timeouts. |
| `.strictmetadata/test-runner/test-runner.toml` | authored | rlsbl | The sandboxed test runner's settings (present only while `rlsbl:test-sandbox` is on). |
| `.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml` | authored | strictspec | The lifecycle-and-license record. |
| `.strictmetadata/changelog/<releasable>/unreleased.jsonl` | authored | rlsbl | Changelog entries since the last release. |
| `.strictmetadata/changelog/<releasable>/<version>.jsonl` | authored, mode 0444 | rlsbl | A released version's entries. |
| `.strictmetadata/changelog/<releasable>/CHANGELOG.md` | generated | rlsbl | A workspace releasable's changelog. A repository with one member writes its changelog to the root `CHANGELOG.md` instead; a workspace's root `CHANGELOG.md` is the roll-up. |
| `.strictmetadata/releases/<releasable>/unreleased.toml` | authored | rlsbl | The next release's intent. |
| `.strictmetadata/releases/<releasable>/v<version>.toml` | authored by the flow | rlsbl | A release archive. |
| `.strictmetadata/releases/<releasable>/version` | authored by the flow | rlsbl | A workspace releasable's version (the Python releasable `version` file). |
| `.strictmetadata/releases/<releasable>/undo-audits.jsonl` | authored by the flow | rlsbl | One line per `release undo`, written before any deletion. |
| `.strictmetadata/batch-releases/unreleased.toml` and `batch-<UTC timestamp>.toml` | authored | rlsbl | The batch release file and its archives. |
| `.strictmetadata/transitions/transitions.jsonl` | authored by operations | rlsbl | The transition record: repository surgery only. |
| `.strictmetadata/history-rewrites/<UTC timestamp>.toml` | authored by the flow | rlsbl | One archive per `release scrub` or `transition declassify` rewrite. |
| `.strictmetadata/release-hooks/<releasable or member name>/<hook>.sh` | authored | rlsbl | Hook scripts a declaration runs (only where the migration moved a customized script). |
| `.strictmetadata/.scaffold-state/scaffold-state.toml` | generated, committed | rlsbl | Managed files with their hashes and the rlsbl version that last scaffolded or released. |
| `.strictmetadata/.scaffold-bases/<path>` | generated, committed | rlsbl | Three-way merge bases. |
| `.strictmetadata/.changelog-validation/<releasable>.toml` | generated, committed | rlsbl | The validation cache. |
| `.strictmetadata/.release-state/` | generated, ignored by its own `.gitignore` (`*` and `!.gitignore`) | rlsbl | Run state: `lock`, `<releasable>/in-progress.toml`, `<releasable>/retry.toml`, `<releasable>/reconcile-plan.toml`, `<releasable>/scrub-result.json`, `batch-plan.toml`. |
| `.git/rlsbl/release-checkout` | outside the tree | rlsbl | The detached release checkout, unchanged. |
| `dev-sources.toml.local-only`, `dev-overlays-state.toml.local-only` | local, ignored | rlsbl | Unchanged: per-machine dev overlays are not records. |

A repository is a workspace when `releasables.toml` declares more than one member; with a single member (the root, `path = "."`, `name = "root"`) it is a standalone project. Every behavior the Python keyed on the presence of `.rlsbl-monorepo/` keys on that count. Old-layout directories (`.rlsbl/`, `.rlsbl-monorepo/`, `<member>/.rlsbl/`) are residue after the migration.

### releasables.toml

Strict TOML, validated by a strictspec schema at `.strictspec/releasables.schema.toml` with its generated Go validator in `internal/declarations`. Unknown keys are refused at every level. Absent optional keys stay absent on rewrite.

```toml
format_version = 1
release_branches = ["main"]                  # required, non-empty
github_repository = "owner/name"             # optional; absent means derived from origin
environment_file = "~/Projects/.env"         # optional; loaded into the release's environment

[timeouts]                                   # optional; each key optional; the shipped value applies when absent
push_seconds = 300
ci_seconds = 3600
check_seconds = 900
hook_seconds = 10800
build_seconds = 120

[[releasables]]
name = "portal"
tag_format = "v{version}"                    # required: no implicit tag scheme
publish_mode = "ci"                          # required: "ci" or "none"
publish_ci_check_pattern = "..."             # required when the root member's releasable publishes from CI in a workspace
deploy_command = ["tool", "deploy", "portal-server", "--version", "{version}"]  # optional; see "Servers"
hooks = { pre_checks = [], pre_release = [], post_release = [] }  # optional; entry: string or { cmd, dir, env }

[[members]]
path = "."
name = "root"
releasable = "portal"                        # a releasable name, or false
dev_only = false                             # optional
library = false                              # optional
test_only = false                            # optional
depends_on = []                              # optional
import_name = ""                             # optional
registry_name = ""                           # optional
description = ""                             # optional
lint_allow = []                              # optional (read by strictcode)
internal_dep_floors = []                     # optional; required while rlsbl:dep-floors is on for this member
targets = [{ name = "go", path = "." }]      # optional; a target with no path uses the member path
hooks = { pre_release = [] }                 # optional
external_checks = [{ name = "x", tag = "preflight", command = "...", depends_on = [], cwd = "." }]
test = { pypi_markers = "", go_command = "" }  # optional; each key optional

[[members.pipelines]]
name = "go"
type = "go"                                  # "go", "npm", or "pypi"
target = "go"                                # required: a target the member declares
local = false                                # required
artifact = "binary"                          # required: go "binary" or "library"; npm and pypi "package" or "go-binary"
install_paths = ["./cmd/portal"]             # go, required when local = true
homebrew_tap = "homebrew-tap"                # go "binary" only, optional
binary_pipeline = "go"                       # npm and pypi "go-binary" only: the go "binary" pipeline it wraps
```

### test-runner.toml

`format_version`, `runner_path`, `command`, `default_args`, `caches` (closed set `uv`, `go`, `python_user_base`), `prewarm`, `extra_env`, `ci_workflows`, `carry_ignored`, with the Python meanings. Required while `rlsbl:test-sandbox` is on and refused while it is off, as in Python.

### Changelog entries

One JSON object per line, `format_version` 2:

```json
{"format_version":2,"id":"<ULID>","commits":["<hash>"],"user_facing":true,"type":"feature","description":"...","batch_reason":"..."}
```

- `id`, `commits`, `user_facing`, `type`, and `description` keep their Python meanings. There is no legacy mode: a line without `format_version` 2 is refused.
- `batch_reason` (optional, non-empty) replaces `batch_limits.exclusions`: an entry carrying it is exempt from `changelog-batch-commits`, and the reason sits on the entry it exempts. `changelog add --batch-reason` writes it. The stale-exclusion clean-up step is deleted with the list.
- The batch limits are fixed in code at five commits per entry and five entries per commit (no repository overrides them).
- Per-version `.md` files are not written; GitHub Release bodies are composed from the JSONL and the archive when they are written.

### Release files and archives

`unreleased.toml`: `format_version = 2`, `bump` (`patch`, `minor`, `major`, `infra`), `include`, `exclude`, `description` (required, non-blank), `context` (optional). `preid` and `blog` are refused (rulings drop the pre-release channel and blog-on-release); a `prerelease` bump is refused.

`v<version>.toml` (the archive): the release file's fields, plus one fate:

- recorded: `release_commit` (the Python `candidate_sha`) and `[released_trees]` (the Python `tree_hashes`, keyed by repository-relative path);
- `unrecoverable = true`;
- `never_released = true`;

and optionally `shipped_as` and `release_notices`, with the Python constraints (one fate, no trees without a recorded fate, no `shipped_as` on a never-released archive). The version is the file name and is not repeated inside.

The batch release file: `format_version = 2` and one `[releasables.<name>]` table per releasable with the single-release fields.

### Transition record

`transitions.jsonl`, one event per line, `format_version` 2, discriminated by `event` (the Python `kind`), with `id`, `recorded_at`, and `related_to` as in Python and a `releasable` field on events that a workspace scopes to one releasable. The events: `conversion`, `tag-map`, `release-commit-remap`, `departed-globs`, and `boundary-alias`. `promotion-split-map` is dropped with the mirror. `identity-transition`, `releasable-rename`, `release-history-closed`, and `non-version-tag` move into the lifecycle-and-license record (ruling).

### Run state

`in-progress.toml`:

```toml
format_version = 1
releasable = "portal"
version = "0.4.0"
previous_version = "0.3.2"
bump = "minor"
tag = "v0.4.0"
branch = "main"
pin_commit = "<sha>"
release_commit = "<sha>"            # present once the candidate is pushed
created_commits = ["<sha>"]
completed_steps = ["version-bumped"]
published_targets = ["npm"]

[[failed_steps]]
step = "pipelines-published"
message = "..."
```

`batch-plan.toml`: one `[[items]]` per releasable with `name`, `base_version`, `target_version`, `tag`, `registry`, and `bump`, as the Python `unreleased.plan.json` holds. `retry.toml`: `ref` (the release tag) and `workflows`. `reconcile-plan.toml`: the Python plan document under the new vocabulary, validated by a generated validator.

### Scaffold state

`scaffold-state.toml` replaces `.rlsbl/managed-files.json` and `.rlsbl/version`: `format_version`, `rlsbl_version`, and `[files]` mapping each managed path to its SHA-256. `publish-cache.json` is not ported: `monorepo sync` always regenerates and compares.

## Config-key audit

Every key of every rlsbl-owned record, with where it goes. "Deleted" keys are refused by the migration only when they carry a value it cannot convert, as stated per row.

### `.rlsbl/config.json` (member, releasable, and standalone configs)

| Key | Goes to | Reason |
| --- | --- | --- |
| `publish_mode` | `[[releasables]].publish_mode` | Declaration of what the releasable is. Members of one releasable disagreeing is refused by the migration. |
| `targets` (strings or `{name, path}`) | `[[members]].targets` | Declaration. |
| `pipelines.<name>` | `[[members.pipelines]]` with `name` | Declaration. |
| `pipelines.*.type` | `type` | `cloudflare-pages` is refused by the migration (ruling drops it). |
| `pipelines.*.local` | `local` | Declaration. |
| `pipelines.*.target` | `target` (required string) | A targetless pipeline existed only for `cloudflare-pages`. |
| `pipelines.*.provenance` | Deleted | Derived: npm build attestations run when, and only when, the repository is public, and are refused when private (ruling on private repositories). |
| `pipelines.*.artifact` | `artifact` | go keeps `binary`/`library`; npm and pypi get `package` (the Python default) or `go-binary`. `launcher` is refused (ruling drops the launcher). |
| `pipelines.*.wraps`, `binary_source`, `download` | Deleted | Launcher only. |
| `pipelines.*.install_paths` | `install_paths` | Declaration. |
| `pipelines.*.assets`, `custom_assets`, `max_asset_size_mb` | Deleted | Ruling drops local asset upload. |
| `pipelines.*.token_var` | Deleted | No config sets it; npm uses `NPM_TOKEN`, PyPI uses Trusted Publishing. |
| `tag` | Option `rlsbl:ecosystem-tagging` | Behavior switch. The migration writes an entry only for `false`. |
| `release_branches` | Top-level `release_branches` (required) | Declaration. Absent becomes `["main", "master"]`, the Python effective value. |
| `changelog_format` | Deleted | One format. |
| `changelog_format_version_enforced` | Deleted | Already retired in Python; the migration stamps every line to `format_version` 2 instead. |
| `private` | Deleted | Already refused in Python. |
| `batch_limits.max_commits_per_entry`, `max_entries_per_commit` | Deleted | No config sets them; fixed in code. |
| `batch_limits.exclusions` | `batch_reason` on the excluded entry | strict.md forbids exemption lists; the reason moves onto the entry. An exclusion naming commits rather than entries is refused (none exists). |
| `push_timeout`, `ci_timeout`, `check_timeout`, `hook_timeout`, `build_timeout` | `[timeouts]` `push_seconds`, `ci_seconds`, `check_seconds`, `hook_seconds`, `build_seconds` | Open values. Configs of one repository disagreeing are refused by the migration, listing the values. A per-target `build_timeout` object is refused (none exists). |
| `test.pypi.markers`, `test.go.command` | `[[members]].test.pypi_markers`, `go_command` | Declaration. |
| `external_checks` | `[[members]].external_checks` | Declaration. The `kind` field is dropped (one form remains). |
| `checks.lint`, `checks.format`, `checks.type-check` | `strictcode.toml` tool tables | Ruling moves them to strictcode. |
| `strictspec_gate` | `strictcode.toml` | Ruling moves the certificate check to strictcode. |
| `test_sandbox` | `.strictmetadata/test-runner/test-runner.toml` | Declaration. |
| `internal_dep_floors` | `[[members]].internal_dep_floors` | Declaration. |
| `env_file` | Top-level `environment_file` | Open value. |
| `github_repo` | Top-level `github_repository` | Open value. |
| `homebrew.tap` | go pipeline `homebrew_tap` | Declaration. |
| `homebrew.description` | Deleted | The member `description` is the one source. |
| `homebrew.license` | Deleted | The record's license is the one source. |
| `hooks` (`pre_checks`, `pre_release`, `post_release`) | `hooks` on the releasable or member that declared it | Declaration. |
| `publish_gate_check_regex` | `[[releasables]].publish_ci_check_pattern` | Declaration; renamed to drop a banned word. |
| `deploy` | Deleted | Ruling drops SSH deploy. |
| `services`, `test_env` | Deleted | Ruling. |
| `npm_wrapper.enabled` | npm pipeline `artifact = "go-binary"` | Ruling keeps the npm wrapper as per-platform packages. |
| `uv_sync_verbose` | Deleted | A diagnostic switch no config sets. |
| User-level `~/.rlsbl/config.json` | Deleted | strict.md forbids per-person settings; `tag` became an option. |

### `.rlsbl-monorepo/workspace.toml` and `.rlsbl/releasable.toml`

| Key | Goes to | Reason |
| --- | --- | --- |
| `[[projects]]` | `[[members]]` | Same table, plain name. |
| member `path`, `name`, `library`, `dev_only`, `releasable`, `depends_on`, `import_name`, `registry_name`, `description`, `test_only`, `lint_allow` | the same keys on `[[members]]` | Declarations. |
| member `watch`, `subtree_remote`, `dev_node` | Refused | Already refused at load in Python; the migration names the fix Python names. |
| `[[releasables]].name` | `[[releasables]].name` | |
| `[[releasables]].tag_format` | `tag_format`, now required | No implicit defaults; the migration writes the Python effective format (`{name}@v{version}` in a workspace, `v{version}` standalone) where it was absent. |
| `[[releasables]].subtree_remote` | Deleted | Mirror dropped; a non-empty value is refused by the migration. |
| `[layers]` | Deleted | Ruling drops layering; the migration deletes the section. |
| `releasable.toml` `name`, `tag_format` | the single `[[releasables]]` table | Standalone declaration. |

### Release files, archives, and other records

| Record and field | Goes to | Reason |
| --- | --- | --- |
| `unreleased.toml` `bump`, `include`, `exclude`, `description`, `context` | same fields | |
| `unreleased.toml` `bump = "prerelease"`, `preid` | Refused | Ruling drops the pre-release channel. A file carrying them is reported, not converted. |
| `unreleased.toml` `blog` | Refused | Ruling drops blog-on-release. |
| archive `format_version`, `bump`, `include`, `exclude`, `description`, `context` | same | |
| archive `candidate_sha` | `release_commit` | Plain name. |
| archive `tree_hashes` | `[released_trees]` | Plain name. |
| archive `unrecoverable`, `never_released`, `shipped_as`, `release_notices` | same | |
| batch file `[releasables.<name>]` | same | |
| batch file `[packages.<name>]` | Refused | Already refused in Python. |
| `unreleased.plan.json` | `.release-state/batch-plan.toml` | Run state; converted only when present. |
| `changes/*.jsonl` lines | `.strictmetadata/changelog/<releasable>/` with `format_version` 2 | Every line is restamped; read-only files are rewritten and set back to 0444. |
| `changes/*.md` | Deleted | Not written any more. |
| `changes/.validated` | Deleted | Rebuilt on the next validation. |
| `CHANGELOG.md` (root, releasable) | regenerated at the new paths | |
| `transitions.jsonl` `conversion`, `tag-map`, `release-commit-remap`, `departed-globs`, `boundary-alias` | `.strictmetadata/transitions/transitions.jsonl`, `kind` renamed `event` | Surgery facts. |
| `transitions.jsonl` `promotion-split-map` | Refused | Mirror dropped; none exists. |
| `transitions.jsonl` `identity-transition` | record `[[identities]]`: the old identity ends, the new one starts | Ruling moves identity facts into the record. |
| `transitions.jsonl` `releasable-rename` | record `[[identities]]` with facet `releasable-name` | Ruling (renamed-from). |
| `transitions.jsonl` `release-history-closed` | record `[[lifecycle]]` period `retired` from `recorded_at` | Ruling. |
| `transitions.jsonl` `non-version-tag` | record `[[unversioned_tags]]` | Ruling (tag ownership). |
| `scrubs/scrub-*.json` | `.strictmetadata/history-rewrites/<timestamp>.toml` | Same fields, TOML. |
| `undo-audit.json` | `.strictmetadata/releases/<releasable>/undo-audits.jsonl` | One line per audit. |
| `retry.toml`, `reconcile-plan.toml`, `in-progress.json`, `scrub-result.json`, `lock` | `.strictmetadata/.release-state/` | Run state; an `in-progress.json` present at migration is refused (finish or abandon the release first). |
| `managed-files.json`, `.rlsbl/version` | `.scaffold-state/scaffold-state.toml` | |
| `.rlsbl/bases/`, releasable `bases/` | `.strictmetadata/.scaffold-bases/` | |
| `.rlsbl/hooks/*.sh`, releasable and member `hooks/` | A script whose content hash matches a known scaffold template is deleted; a customized script moves to `.strictmetadata/release-hooks/<owner>/` and a declaration running it is added to the same owner's `hooks`. | The Python compatibility path that read script hooks is not ported. |
| `.rlsbl/lint/*.toml` | `strictcode.toml` tables of the library rules, per language: forbidden and allowed imports to `library-forbidden-imports` (`forbidden`, `allow`), the stdout settings to `library-stdout`, the entry-point settings to `library-entry-point`, ignore lists and exclude patterns to suppressions, each with its reason | Ruling moves `library-lint` to strictcode. A setting with no strictcode counterpart is refused, naming it. |
| `rlsbl:ruff-lint` (the check, on a member with a pypi target) | `strictcode.toml` `[python_tools.lint]` with the member's path, written unless the member's `rlsbl:ruff-lint` entry is `off` | Ruling merges `ruff-lint` into strictcode's `lint` rule, whose table's presence is the choice. |
| `.rlsbl/dead-modules.toml` | `strictcode.toml` `dead-modules` suppressions, each with its reason | Ruling moves dead-modules. |
| `.rlsbl-monorepo/snapshot.json` | Deleted | Ruling drops the snapshot. |
| `.rlsbl-monorepo/publish-cache.json` | Deleted | Not ported. |
| releasable `version` | `.strictmetadata/releases/<releasable>/version` | |
| `.strictmetadata/options/*.toml` rlsbl entries | rewritten for renamed options; refused for removed options with no home | strictspec-owned; only rlsbl's entries are touched. |

## The lifecycle-and-license record and its library

### Where the library lives

A new package `lifecycle` in strictspec's Go module: import path `github.com/stricttools/strictspec/go/lifecycle`. strictspec owns the record's directory (strict.md: a directory several tools share is owned by strictspec), so the schema and the code that reads it sit in the same module, and rlsbl, selfdoc, and safegit link it through a dependency they already have or that costs nothing new. Its schema is a strictspec built-in, `go/strictspec/builtin/lifecycle-and-license.schema.toml`, embedded the way the options schemas are. This adds strictspec to the release order (see "Release order").

### The record

`.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml`, with `manifest.toml` naming strictspec:

```toml
format_version = 1
codenames = []                        # confidential repositories: extra names to protect
distinctive_terms = []                # confidential repositories: prose terms to protect

[[lifecycle]]
subject = "portal"                    # a releasable or member name (names, never stable IDs)
status = "active"                     # "active", "on-hold", or "retired"
from = 2026-10-07                     # TOML local date
until = 2027-01-01                    # optional; absent while current
reason = "..."

[[licenses]]
subject = "portal"
license = "MIT"                       # an SPDX identifier, or "proprietary"
from = 2026-10-07
until = 2027-01-01                    # optional; a future `from` schedules a change
reason = "..."

[[identities]]
subject = "portal"
facet = "releasable-name"             # "releasable-name", "package-name", "go-module-path", "repository-url"
value = "portal"
registry = ""                         # "npm", "pypi", "go", or "" when the identity is not a registry name
tag_patterns = ["v*"]                 # the tag namespace the identity owned while its period ran
from = 2026-10-07
until = 2027-01-01                    # optional
reason = "..."

[[registry_names]]
registry = "npm"
name = "portal"
subject = "portal"
recorded_since = 2026-10-07

[[unversioned_tags]]
tag = "nightly"
reason = "..."
recorded = 2026-10-07
```

A repository is confidential when one of its releasables has a `proprietary` license period in effect, and public otherwise; no field declares it, and every tool (safegit included) derives it from the license periods offline. A repository without a record is public, and confidential names are refused there. Private repositories without a proprietary releasable are not designed for: `repository-visibility` refuses them.

Validation on the record alone: periods of one subject and table do not overlap and at most one is open; `codenames` and `distinctive_terms` are refused while the repository is public; a subject must be a releasable or member name the repository's declarations hold, or the subject of a closed period.

### The rules

The rule set is closed and fixed in code as `lifecycle.Rule` constants with one evaluation function each. Callers ask typed questions; nothing in the record names a rule.

| Rule | Type | What it decides | Enforced by |
| --- | --- | --- | --- |
| `proprietary-requires-private` | while a value holds | A releasable whose current license is `proprietary` makes the repository confidential and requires GitHub's visibility of the repository to be private. | rlsbl release validation and `repository-visibility`. |
| `proprietary-refuses-public-output` | while a value holds | A proprietary releasable publishes to no registry, gets no build attestation (npm `--provenance`, PyPI attestations), gets no public docs deploy, and gets no blog post. | rlsbl release validation (pipelines of that releasable); selfdoc deploy and blog (content scan below). |
| `private-repository-publishing` | while a value holds | In a confidential repository: no npm build attestations (`--provenance`), no PyPI attestations, no Go module proxy notification, no Go `library` pipeline, no Homebrew tap, and no published manifest field that names the repository (`package.json` `repository`, `homepage`, `bugs`; `pyproject.toml` `[project.urls]`). | rlsbl release validation, `private-repo-publishing`, and the generated publish workflows (scaffold renders attestations off). |
| `lifecycle-allows-release` | while a value holds | A subject whose current status is `on-hold` or `retired` is not released: its release is refused; a retired subject's release state is a record, not residue. | rlsbl release validation, batch release planning, `releasable-residue`. |
| `confidential-names` | while a value holds | While the repository is confidential, its names go into the machine-local index: every proprietary releasable's names and identity values, the repository name when no releasable carries a non-proprietary license, the codenames, and the distinctive terms. | safegit commit; rlsbl published text and packed artifacts; selfdoc deploy and blog output. |
| `proprietary-history-is-squashed` | over what was created during a period | Before the repository is declassified, the commits of each proprietary period (the union of the releasables' proprietary license periods, taken as disjoint date ranges, over the release branch's first-parent history by committer date) are squashed into one commit per period whose message names no confidential term; the public history before and after each period is kept. | `transition declassify`. |
| `identity-owns-its-tags` | over what was created during a period | A tag matching an identity's `tag_patterns`, created while that identity's period ran, belongs to that identity; after the period closes such tags are accounted for: never demanded, never unexplained, never scrubbed, and never refused. | rlsbl `release backfill`, `release reconcile`, `unpublished-refs`, `mixed-tag-schemes`, `go-companion-tags`. |
| `registry-names-are-held` | permanent once triggered | A registry name, once recorded, stays in the record; no rlsbl command unpublishes or deletes a package (yank deprecates and retracts only). | `lifecycle-record-valid` (append-only against the nearest release commit). |
| `closed-periods-are-final` | permanent once triggered | A closed period of any table is never changed or removed. | `lifecycle-record-valid`. |

### The confidential-name index

- A machine-local TOML file outside every repository: `<os.UserConfigDir()>/strictspec/confidential-names.toml`, one `[[repositories]]` table per confidential repository with `origin` (the normalized origin URL) and `names` (the `confidential-names` rule's output). The library owns the format; the file is created on first write.
- Every linking tool upserts the current repository's entry whenever it loads the record of a confidential repository (rlsbl on every command that loads declarations, selfdoc on every command, safegit on every commit), and removes it when it loads the record of a public repository, or finds no record, for that origin. The migration populates it for every repository it converts.
- Matching is case-insensitive on whole tokens (a name matches where it is bounded by characters outside `[A-Za-z0-9_-]`). The library exposes `index.Scan(text) []Match` with the term, the line, and the column.

### Library API

- `lifecycle.Load(repoRoot string) (*Record, error)` (a missing file yields an empty record, which is public; `(*Record).Present() bool` tells it apart for `lifecycle-record-valid`), `(*Record).Write(repoRoot string) error` (go-toml-edit, comments kept), and the record mutators the `transition` commands use: `OpenPeriod`, `ClosePeriod`, `AddIdentity`, `AddRegistryName`, `AddUnversionedTag`.
- Questions: `Confidential(on time.Time) bool` (true when a releasable's license period on that date is `proprietary`), `ReleaseAllowed(subject string, on time.Time) error`, `PublishAllowed(subject string, output Output, visibility Visibility) error` (outputs: `RegistryPackage`, `BuildAttestation`, `GoProxyNotification`, `GoLibrary`, `HomebrewTap`, `RepositoryURLInManifest`, `PublicDocs`, `BlogPost`), `ConfidentialNames() []string`, `TagOwner(tag string, created time.Time) (Identity, bool)`, `ProprietaryPeriods() []Period`, `CheckAppendOnly(previous *Record) error`.
- `lifecycle/index`: `Load`, `Upsert(origin string, names []string)`, `Remove(origin string)`, `Scan(text string) []Match`.
- Every refusal is an error value naming the rule, the subject, the period, and what to do.

### rlsbl's `transition` commands

- `transition lifecycle`, `transition license`, `transition identity`, and `transition unversioned-tag` close the open period on the command's date and open the new one, validate the whole record, write it, and commit it alone with the `Autogenerated: true` trailer.
- `monorepo rename-releasable` closes the old `releasable-name` identity and opens the new one (with the tag pattern rendered from `tag_format`) in the same commit it already makes; `rewrite project-name` does the same for `package-name` and `go-module-path`, effective at the next version as in Python (the record holds dates; the commit date of the next release closes the old identity, written by the release at its archive step when an identity period is pending).
- `transition classify --subject <releasable>`: refuses unless GitHub visibility is already private; opens a `proprietary` license period (which makes the repository confidential), records the names in the index, and commits. Making the GitHub repository private is the owner's action outside rlsbl, named in the refusal.
- `transition declassify`: refuses unless one `--license` names each releasable whose current license is `proprietary` and no other releasable. It collects the confidential terms from the record as it stands, then, in this order: squashes each proprietary period's commits (as `proprietary-history-is-squashed` defines the periods) into one commit whose message is a fixed text naming no confidential term, through the squash mode of `safegit scrub` that S22 adds, keeping the public history before and after each period (a period whose commits are not contiguous on the first-parent history is refused before anything is rewritten, naming the commit that interrupts it); runs the release metadata repair that `release scrub` runs (changelog hashes, release commits, tags, Release bodies), where the remap is many-to-one: a release tag inside a squashed period is re-pointed to that period's squash commit, and its archive becomes `unrecoverable`, because the released tree no longer exists; closes the proprietary license periods and opens the licenses `--license` names; removes `codenames` and `distinctive_terms`; removes the index entry; commits; and makes the GitHub repository public through `gh repo edit --visibility public --accept-visibility-change-consequences`. A history-rewrite archive records the run.
- `transition init-minimal-record`: for a repository rlsbl does not manage. Refuses where `.strictmetadata/releasables/releasables.toml` or an old-layout directory exists (rlsbl manages the repository; the migration or `transition` commands write its record) and where a record already exists. Writes `.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml` holding only `format_version = 1`, and its `manifest.toml` naming strictspec (with no license period, the repository is public), and commits both with the `Autogenerated: true` trailer. It loads no declarations, and `lifecycle-record-valid` accepts a record without subjects.

### Enforcement in selfdoc and safegit

- selfdoc: `selfdoc deploy` and blog publication load the record of the repository; in every repository (public, confidential, or without a record) every page and post is scanned with `index.Scan` and any match refuses the deploy or post, naming page, line, and term. A repository whose record has no releasable that may publish (every current license proprietary) refuses the deploy outright under `proprietary-refuses-public-output`. Every selfdoc command upserts the index.
- safegit: `safegit commit` (and `commit --amend`) in a public repository, a repository without a record included, scans the message, every added or changed line, and every new path with `index.Scan`; a match refuses the commit, naming file, line, and term. A confidential repository is not scanned. safegit decides confidentiality from the record's license periods offline, through `lifecycle.Load` and `Confidential`, never by asking GitHub.
- safegit: `safegit scrub` gains a squash mode that rewrites one first-parent commit range into one commit with a given message and records every squashed commit against the squash commit in its rewrite journal, so `changelog remap` and `release reconcile` can follow it. `transition declassify` is its caller.

## Clients, servers, and publishing rules

- **Servers.** A releasable that declares `deploy_command` is a server releasable. It must declare `publish_mode = "none"` (registries only ever receive the client; axiom). After the tag is pushed and the GitHub Release exists, the step `deployed` runs the argv with `{version}` replaced, from the repository root, through the effects handle with `check_seconds` as its timeout. A non-zero exit fails the release and leaves it resumable; `release resume` reruns the deploy for the same version. The command's own host, health, and rollback logic are not rlsbl's. Batch releases order a server after the clients it depends on, by `depends_on`.
- **Per-releasable licenses** come from the record; the private-repository and no-public-output rules are evaluated per releasable, so a confidential repository may hold a releasable under a public license whose client is published.
- **The private-repository publishing guard** is the `private-repository-publishing` rule in the library; `internal/publishrules` supplies GitHub's visibility and applies it at release validation, in `private-repo-publishing`, and in scaffold (publish workflows for a confidential repository are rendered without build attestations (npm `--provenance`, PyPI attestations), proxy notification, or repository URLs).
- **Go clients of private repositories** ship as binaries only, through npm platform packages and PyPI binary wheels; a `library` go pipeline in a confidential repository is refused.
- **The packed-artifact contents check** runs at release validation after the build step, for every releasable with a publishing pipeline: npm packages are listed with `npm pack --dry-run --json --ignore-scripts`; Python wheels are built by the existing build step and each entry outside `*.dist-info/` is matched by content to a file under the releasable's member paths; Go binaries are listed with `go list -deps -json` over each `install_paths` main package (every package directory and embedded file inside the repository). Any file from outside the releasable's member paths refuses the release, naming each file. Every text file in those artifacts, the GitHub Release body, and the published `CHANGELOG.md` section are scanned with `index.Scan` in public repositories.

## How each subsystem maps to Go

Each item names the Go package and what changes beyond moving to the new layout and the effects handle.

- **Release run and resume** (`internal/release`): the Python step table becomes `release.Steps`, an ordered table with each step's name, fatality, and plan entries: `version-bumped` (plan entries: write releasable version, write target versions, write member versions, bump selfdoc, ensure keyword, sync lockfiles, write scaffold state, clean artifacts, build, secret scan, packed-artifact contents, guard unexpected files), `committed`, `candidate-pushed`, `ci-verified`, `changelog-finalized`, `release-archived`, `tagged`, `pushed`, `github-release-created`, `pipelines-published`, `deployed`, `post-release-hooks-run` (the one non-fatal step). Dropped steps: snapshot, subtree publication, mirror release, asset upload, the old deploy. The range pin, the four checkpoints, foreign-commit refusal, main-as-candidate ordering, the four CI verdicts, resume re-pinning, and the release checkout are ported as they are. Validation adds the lifecycle checks (`lifecycle-allows-release`, `proprietary-requires-private`, `proprietary-refuses-public-output`, `private-repository-publishing`), the record's append-only check, and the packed-artifact contents check. Preflight checks always run, built-in and external alike, whether or not pre-release hooks are declared; a declared pre-release hook replaces only the built-in tests. The Python skip of built-in checks when a hook is customized is not ported; `release.preflightSelection` holds this rule in one place, with tests for a release with and without a declared pre-release hook.
- **Release init, retry, edit, undo, abandon, deprecate, yank** (`internal/release`, `internal/releaseops`): ported. `undo --version` takes its evidence from the repository's records and GitHub (the tag on origin, the Release, publish runs for the tag) and from package-level registry listings; it never requests one version's metadata (see "Registry lookups"). `yank` keeps npm deprecate, Go retract, and the PyPI checklist, and never unpublishes (`registry-names-are-held`).
- **Scrub, backfill, reconcile** (`internal/historyrewrite`): ported; tag accounting adds the record's identities and unversioned tags as explanation sources; the `promotion-split-map` source is dropped; scrub archives go to `.strictmetadata/history-rewrites/`.
- **Changelog** (`internal/changelog`): ported with the format changes above. The workspace roll-up writes one `# Changelog` heading with one `## <releasable>` section each and versions one level below, fixing the defect filed in `todo/workspace-changelog-rollup-emits-one-h1-per-releasable.md` (red test first).
- **Monorepo** (`internal/monorepo`, `internal/workflows`, `internal/batchrelease`): ported. `graph` gains `--json` (members, versions, targets, and edges in topological order). `extract` keeps the filter engine and drops promotion. `absorb` writes the arriving repository's records into the new layout. `cleanup` removes old-layout residue (`.rlsbl/`, `.rlsbl-monorepo/`, member `.rlsbl/`, and an absorbed repository's own `.strictmetadata/` release directories inside the member), through saferm, and its finding is `releasable-residue`. `check-names`, `outdated`, `impact`, `rename-releasable`, and batch `run`, `init`, `order` are ported.
- **Scaffold** (`internal/scaffold`): the Python template placeholder grammar (`{{name}}`, `{{action "owner/repo"}}`, `{{actionVersion "x"}}`, and the block placeholders) is ported in `render.go`; GitHub's `${{ ... }}` passes through untouched. Templates move under `internal/scaffold/templates/` with the dropped ones deleted (`spec/`, `npm/publish-launcher.yml.tpl`, `npm/shim-*.cjs.tpl`, `pypi/publish-launcher.yml.tpl`, `pypi/shim-launcher.py.tpl`, `shared/.github/workflows/deploy.yml.tpl`). The publish workflows' waiting job is renamed `wait-for-ci`. The three-way merge keeps `git merge-file` and healing from the last scaffold commit. `LICENSE` is written only from the record's current license, for licenses whose text the scaffold embeds, and never without a record. The npm platform package templates take the license from the record instead of the literal `MIT`. The version-agreement refusal (commit 49ace6c3, `rlsbl/commands/init_cmd.py` `_refuse_new_version_disagreement`, with `tests/test_scaffold_version_agreement.py`) is ported as `scaffold.refuseNewVersionDisagreement` with every Python test case.
- **Checks framework** (`internal/checks`, `internal/options`): strictcli's check framework with the embedded registry; check values come from options entries through `SetCheckValueResolver`; external checks keep the release-context environment (`RLSBL_PROJECT_ROOT`, `RLSBL_LAST_TAG`, `RLSBL_UNRELEASED_RANGE`).
- **Options** (`internal/options`): ported; `options registry` prints the generated document; `options set` writes through strictspec/go's validation.
- **Dev** (`internal/devtools`), **rewrite** (`internal/rewrite`), **upstream** (`internal/upstream`), **secrets** (`internal/secrets`), **discover** and tagging (`internal/tagging`), **watch** (`internal/ci`), **status**, **unreleased**, **targets**, **commit** (`internal/release`), **check-name** and **claim-name** (`internal/registry`): ported. `rewrite go-module-path` locates import sites with `go/parser` (`parser.ImportsOnly`) and rewrites by line, keeping the boundary rules.
- **The release checkout**: unchanged in location and behavior.
- **CI router and publish workflow generation** (`internal/workflows`): ported; npm `go-binary` pipelines render one job per platform that packs the goreleaser binary into its platform package and publishes it, then the main package with `optionalDependencies` pinned to the version, `os` and `cpu` fields, a `bin` launcher that resolves the platform package (the Python `bin-index.js.tpl`), and no install scripts; pypi `go-binary` pipelines render a job that assembles one wheel per platform (`py3-none-` plus `manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64`, `manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64`, `macosx_10_12_x86_64`, `macosx_11_0_arm64`) with the binary under `<name>-<version>.data/scripts/`, `METADATA`, `WHEEL`, and `RECORD`, in POSIX shell with `sha256sum`, `base64`, and `zip`, and publishes them through Trusted Publishing. The platform set is the npm platform table in `internal/pipelines` (linux x64 and arm64, darwin x64 and arm64; no win32 platform), one table for both registries. An npm platform package is named `<main package>-<os>-<cpu>` from that table (for rlsbl: `rlsbl-linux-x64`, `rlsbl-linux-arm64`, `rlsbl-darwin-x64`, `rlsbl-darwin-arm64`). The names this derives for any other project (saferm's included) are new registry names, confirmed by the owner before that project's first release with platform packages.
- **The private-paths rule in CI**: the rule tables live in `internal/targets/privatepaths.go`; a generator renders a standalone Go program from them into the pypi CI template, which the workflow runs with `go run` after `actions/setup-go`; a freshness test keeps the two in step. The Python module embedded in the template is not carried.
- **The support matrix**: kept (ruling). `internal/targets/gen` writes `internal/targets/support-matrix.json`; `target-matrix-fresh` compares; the docs directives under `.strictmetadata/docs/directives/` read the new path (they stay the scripts selfdoc runs; only their input paths change).
- **Registry lookups**: rlsbl never requests the metadata of one version of a package or module it might own (`~/Projects/CLAUDE.md`: a lookup can burn a version). npm and PyPI reads use the package document that lists every version (`https://registry.npmjs.org/<name>` and `https://pypi.org/pypi/<name>/json`); Go reads use `https://proxy.golang.org/<module>/@v/list` only; `sum.golang.org` is never contacted. A package with no document is unpublished. `undo --version` evidence and the registry probe of `rewrite uv-path-sources` read these listings of already-published versions and decide from them whether a version is published; no request ever names one of our unpublished versions. Both get red tests that assert no version-specific URL is requested.

## How rlsbl calls strictcode

- As a binary on `PATH`, never as a library: rlsbl analyzes no source and takes no tree-sitter dependency.
- The `strictcode` check runs `strictcode version --json` (strictcli's framework version command, whose document carries `name` and `version`) first and refuses a missing binary or a version below `checks.MinimumStrictcodeVersion` (the version this campaign releases), naming the install command `go install github.com/smm-h/strictcode/cmd/strictcode@v<version>`; then it runs `strictcode analyze <repository root> --json` through the effects handle with `check_seconds` as its timeout, renders each finding (rule, path, message), and fails on any error-severity finding or a non-zero exit.
- strictcode reads the members from `.strictmetadata/releasables/releasables.toml` and its own `strictcode.toml`; rlsbl passes nothing else.

## The strictcode port

Done in `~/Projects/strictcode`, module `github.com/smm-h/strictcode`, before rlsbl's Python is deleted (ruling). strictcode already implements `deps-unused`, `deps-undeclared`, `deps-runtime-test-only`, `deps-dev-in-production`, `dead-modules`, `dead-workspace-packages`, `import-cycles`, and `stale-suppression`. The port:

| rlsbl check | strictcode rule | Work |
| --- | --- | --- |
| `deps-unused`, `deps-undeclared`, `deps-runtime-test-only` | same IDs | Port every case from `tests/test_dep_validation.py` and `tests/test_deps_stale.py` that the lessons register lacks; the import-name mismatch from `todo/deps-checks-import-name-mismatch.md` gets a red lesson first. |
| `deps-dev-in-lib` | `deps-dev-in-production` | Port the cases. |
| `deps-stale` | new `deps-stale` | Intra-workspace constraints satisfied by the dependency's current version (`constraints.py` semantics), manifest-level. |
| `dead-modules` | `dead-modules` | Port the cases of `tests/test_import_scanners.py` and the dead-module tests, and the recent fixes as lessons: other packages' Go tests count as uses and `package main` is an entry point (commit cd914b28), testdata and test-only Go packages excluded (commit 7ff073a5), source walks follow what git lists (commit c2fbd50d), a member's walk leaves nested members out (commit de95380c), plus the filed false positives (`todo/dead-modules-src-layout-false-positive.md`, `todo/dead-modules-false-positives-on-typescript-sources.md`, `todo/dead-modules-prunes-directories-named-build.md`), each a red lesson first. |
| `dead-modules-stale` | `stale-suppression` | `dead-modules.toml` entries become `dead-modules` suppressions with reasons. |
| `circular-deps` | `import-cycles` | Port `tests/test_circular_deps.py` and `tests/test_circular_deps_fix.py` cases. |
| `lint`, `format`, `type-check` with their scope guards | new `lint`, `format`, `type-check`, `lint-scope-guard`, `format-scope-guard`, `type-check-scope-guard` | Run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` over the paths a `[python_tools.<rule>]` table in `strictcode.toml` declares (`paths` required, `cwd` optional); with no table the rule reports nothing (presence is the choice, as strictcode's analysis modes already work). Port `tests/test_tool_checks.py`. |
| `ruff-lint` | merged into the new `lint` | `lint` takes over `ruff-lint`'s ruff version floor and its parsing of ruff's JSON output, and reports each ruff finding; the migration writes the `[python_tools.lint]` table for every member `ruff-lint` covered (see the config-key audit). Port the `ruff-lint` cases of `tests/test_nested_member_walks.py` and `tests/test_target_axis_check_scopes.py`. |
| `library-lint` | `library-forbidden-imports`, `library-stdout`, `library-entry-point` | Port every case of `tests/test_lint*.py` (Python, npm, and Go libraries, the member `lint_allow` list, the per-language settings, nested-member exclusion) that the lessons register lacks; the per-language settings of `.rlsbl/lint/<language>.toml` become those rules' `strictcode.toml` tables (see the config-key audit). |
| `dead-workspace-packages` | same ID | Port the cases of `tests/test_dep_validation.py` and `tests/test_per_member_checks.py` that name it and the lessons register lacks. |
| `strictspec-certificate-gate` | new `strictspec-certificate` | Consume a `strictspec diff` certificate and optional adjudication file declared in `[strictspec_certificate]` of `strictcode.toml`; port `tests/test_strictspec_gate.py`. |

Also: strictcode reads members from `.strictmetadata/releasables/releasables.toml` (its `internal/workspace` reads `.rlsbl-monorepo/workspace.toml`); the registry dump and support matrix are regenerated in the first-run phase; the release builds and publishes binaries (goreleaser, `CGO_ENABLED=0`) so rlsbl's users can install it without a Go toolchain where one is absent. strictcode is released after the cgofree tree-sitter modules it requires (see "The cgofree tree-sitter modules").

### The cgofree tree-sitter modules

strictcode requires four cgofree modules that are unreleased at this plan's commit: its `go.mod` names them at the placeholder version `v0.0.0-00010101000000-000000000000`, and its local, gitignored `go.work` points them at the checkouts under `~/Projects/stricttools/tools/cgofree/generated/`. They are released before strictcode as subdirectory modules of the cgofree repository (`github.com/stricttools/cgofree`), named after the directories they already occupy; no new repository and no new registry name is created.

| Directory in the cgofree repository | Module path | Tag |
| --- | --- | --- |
| `generated/tree-sitter` | `github.com/stricttools/cgofree/generated/tree-sitter` | `generated/tree-sitter/vX.Y.Z` |
| `generated/tree-sitter-go` | `github.com/stricttools/cgofree/generated/tree-sitter-go` | `generated/tree-sitter-go/vX.Y.Z` |
| `generated/tree-sitter-python` | `github.com/stricttools/cgofree/generated/tree-sitter-python` | `generated/tree-sitter-python/vX.Y.Z` |
| `generated/tree-sitter-typescript` (packages `typescript` and `tsx`) | `github.com/stricttools/cgofree/generated/tree-sitter-typescript` | `generated/tree-sitter-typescript/vX.Y.Z` |

Every version is 0.x (the first is `v0.1.0`); no 1.x tag is ever created for any of them.

Making them releasable (S24, files only):

- cgofree's `.gitignore` ignores all of `/generated/`; it is narrowed so these four directories are tracked, and every other generated checkout stays ignored.
- Each directory's `go.mod` takes the module path in the table; the three grammar modules require the runtime under its new path, and every import of `github.com/cgofree/tree-sitter...` in the four directories is rewritten to the new path.
- The cgofree code that produces these paths follows: the module-path rule in `internal/recipe/recipe.go` (a recipe of `recipes/` generates `github.com/stricttools/cgofree/generated/<name>` instead of `github.com/cgofree/<name>`, red test first), the schema description generated into `internal/recipe/cgofree_recipe_gen.go`, the `import_path` entries of `record.toml`, and the `GOPRIVATE` list in `internal/pipeline/pipeline.go`. cgofree's `docs/decisions.md` records that these modules are released from the cgofree repository rather than from repositories of the cgofree organization.
- In strictcode: every import is rewritten to the new paths, `go.mod` names the new paths at the placeholder version, and the local `go.work` `use` and `replace` lines move to the new paths, so strictcode keeps building against the checkouts until the releases exist.

Declaring and releasing them is in "After the build": rlsbl declarations for cgofree, written with the Go rlsbl's own commands, make a workspace whose root member is a dev node, with one member and one releasable per directory, each with a go `library` pipeline and `tag_format = "generated/<directory name>/v{version}"`, the grammar members declaring `depends_on` the runtime; its lifecycle-and-license record takes each releasable's license from the owner, never from a `LICENSE` file. Once they are released, strictcode's `go.mod` requires each one by its released version and the `go.work` `use` and `replace` lines for them are deleted. No request ever names a version of these modules before it is published.

## Distribution of rlsbl

- The Go module `github.com/stricttools/rlsbl` (`go install github.com/stricttools/rlsbl/cmd/rlsbl@v<version>`).
- GitHub Release archives built by goreleaser in CI (the go `binary` pipeline).
- npm: the existing `rlsbl` package becomes the main package of an npm `go-binary` pipeline, with one platform package per platform in the table above: `rlsbl-linux-x64`, `rlsbl-linux-arm64`, `rlsbl-darwin-x64`, and `rlsbl-darwin-arm64`. There are no win32 packages.
- PyPI: the existing `rlsbl` project receives the platform wheels of a pypi `go-binary` pipeline; `pyproject.toml` keeps only `[project]` metadata (name, version, description, license, urls) that the wheel job reads.
- The three version files (`VERSION`, `package.json`, `pyproject.toml`) agree, enforced by `version-consistency`.

## Test strategy

- Tests are written in every slice beside the code (`internal/<package>/*_test.go`) and run first in the first-run phase.
- Every `Test` function calls `hygiene.Isolate(t)` as its first statement; `internal/testsupport/isolation_test.go` parses every test file in the module with `go/parser` and fails on a test that does not (an earlier Go rewrite found `TestMain` cannot bind the isolation).
- Commands are tested in process through strictcli's `App.Test`, in a fixture repository under `t.TempDir()` with a `file://` bare remote (testisolation locks transports to `file`).
- `gh` is faked by the test binary itself: `testsupport.FakeGH(t, fixture)` writes a symlink named `gh` to `os.Args[0]` in a temporary `PATH` directory and the fixture beside it; `TestMain` in each package that uses it dispatches on `filepath.Base(os.Args[0]) == "gh"`, answers from the fixture, and appends each invocation to a calls file the test asserts on. No environment variable carries test input.
- Registries and the Go proxy are faked by an `http.RoundTripper` passed through strictcli's `WithHTTPClient`; no test opens a socket.
- Ported tests follow the intent of the Python test files named in each slice; a Python test that exercised a dropped subsystem is not ported.
- Every bug fixed during the port gets a red test first (red-green), and integration tests cover the release path end to end against fixture repositories and fakes (release run to `github-release-created`, resume after a red verdict, batch release, scrub with a fake safegit journal, migration of a fixture repository in each old layout).
- The suite runs through `scripts/go-suite.sh` (`go vet ./...`, then `go test -p 2 -parallel 2 -race -timeout 40m ./...`), and in CI and locally inside the sandboxed runner declared in `test-runner.toml` (`command = "scripts/go-suite.sh"`, `caches = ["go"]`).
- Committed generated artifacts each have a freshness test: the help document (`.strictmetadata/.cli-schema/schema.json` against `rlsbl help --json`), the CLI test-coverage manifest, the options registry, the support matrix, the Go standard-library table, the private-paths CI program, and every strictspec-generated validator.

## The record migration

- Command: `rlsbl migrate records`, in the source-built binary only. It converts the repository whose git root contains the working directory; there is no multi-repository fan-out and no implicit target.
- Inputs: the old layout and `--licenses <file>` when a releasable's license cannot be read from its manifests (a TOML map from releasable name to an SPDX identifier or `proprietary`). It never infers a license from `LICENSE` text and refuses, naming each releasable, when one is missing.
- What it converts: every row of the config-key audit; the changelog (restamped, moved, read-only files restored to 0444, `.md` files deleted); archives and release files; batch files; the transition record (surgery events moved, the rest folded into the record); the lifecycle-and-license record, written for every repository it converts (one `active` lifecycle period and one license period per releasable from the migration date, retired periods from `release-history-closed`, identities from the current names and tag formats plus the converted renames and identity transitions, registry names for every releasable with `publish_mode = "ci"` and at least one recorded release, unversioned tags); scaffold state and bases; hook scripts; options entries; `dead-modules.toml`, `.rlsbl/lint/*.toml`, the `[python_tools.lint]` tables that replace `ruff-lint`, and the tool settings into `strictcode.toml` (written beside existing content, or created); deletion of snapshot and publish-cache files; the confidential-name index entry.
- `--dry-run` prints, per file, the source path, the destination path, and the change, plus every refusal, and writes nothing.
- Refusals, all before anything is written: an `in-progress.json` present; an uncommitted change to any path it reads or writes (other uncommitted paths are listed and left alone, since other sessions may be editing the repository); any value the audit marks refused; a missing license; timeouts or publish modes that disagree within the repository; a target name not in go, npm, and pypi (the Python already refuses these; the owner ruled such projects are fixed when next released, and the migration is that point, so the refusal names the hand edit that removes the target and its pipeline); a workspace with a retired key; an old layout it does not recognize.
- Commit: one `safegit commit -m "Migrate rlsbl records to the .strictmetadata layout" -- <every path written or deleted>` per repository, listing only rlsbl-owned paths (old and new), with the `Autogenerated: true` trailer. Deletions of old files go through saferm before the commit.
- Idempotent: a converted repository has nothing to do and says so.
- The repository list comes from `scripts/list-rlsbl-repositories.sh`: it walks `~/Projects` (skipping `~/Projects/.archive`, `node_modules`, `experiments/`, `.git/rlsbl/release-checkout`, and `*.local-only` paths) for `.rlsbl/config.json`, `.rlsbl/releasable.toml`, and `.rlsbl-monorepo/workspace.toml`, prints each distinct git root once, and has `--dry-run` that prints what it found and why each was included. The list is never written into any committed file.

## Build slices

Each slice is one implementor session. An auditor that reads only this plan, the decisions file, the deviations file, and the code checks each slice against its done criteria. Slices on the same line of the dependency list may run in parallel (at most six at once).

Dependencies: S1 first; then S2, S3, S6; then S5, S7, S8; then S4; then S9, S10, S11, S12; then S13; then S14, S15, S16, S17, S19; then S18, S20; S24 at any point, before S21; S21 and S22 after S6 (and S21 after S3 for the declarations file shape); S23 last.

**S1. Foundation.** `go.mod`, `go.work`, `cmd/rlsbl/main.go`, `internal/cli` (app construction with `WithChecksEmbed` of an empty registry, `WithSourceTreeRoot`, `WithTestCoverage`, the registration helpers, the renderers), `internal/testsupport` (fixture repositories, fake gh, fake HTTP, isolation guard test), `internal/semver`, `internal/git`, `internal/saferm`, the layering test, `scripts/go-suite.sh`, and the guard test refusing the word "kind" in identifiers (parses every Go file). Ports from: `utils.py` (version and git parts), `git_util.py`, `saferm.py`, `tests/conftest.py`, the version and git tests (`tests/test_*version*`, `tests/test_utils_git.py`, `tests/test_githarness.py`). Done: every listed file exists; `internal/git` covers every git operation the Python names in `git_util.py`; the isolation and layering guards exist; no file outside `internal/cli` registers a command.

**S2. GitHub, registries, naming, tagging.** `internal/github`, `internal/registry`, `internal/tagging`; commands `check-name`, `claim-name`, `discover`. Ports from: `utils.py` (`run_gh` family), `registry.py`, `package_names.py`, `go_package_name.py`, `data/go-stdlib-packages.json`, `commands/check.py`, `commands/claim_name.py`, `commands/discover.py`, `tagging.py`, and their tests (`tests/test_check_name*`, `tests/test_claim*`, `tests/test_discover*`, `tests/test_tagging*`, `tests/test_registry*`). Done: registry reads use only the package-level URLs; a test asserts no version-specific URL is built; check-name exit codes match the Python (0, 1, 2).

**S3. Declarations and the workspace model.** `.strictspec/releasables.schema.toml`, `.strictspec/test-runner.schema.toml`, their entries in `strictspec.toml` with `lang = "go"` targets in `internal/declarations`; `internal/declarations` (strict load, write with go-toml-edit, the refusals); `internal/workspace`. Generated validators: **coordinator step** (`strictspec gen`); until then the loader calls the validator through the identifiers strictspec's Go emitter produces, as selfdoc's generated validators show. Ports from: `config.py`, `workspace.py`, `workspace_types.py`, `member_context.py`, `resolved_target.py`, `ownership.py`, `tag_glob.py`, `workspace_graph.py`, `constraints.py`, and `tests/test_config*`, `tests/test_workspace*`, `tests/test_ownership*`, `tests/test_tag_scheme.py`, `tests/test_tag_owner_mapping.py`, `tests/test_derive_releasable_tag_format.py`, `tests/test_monorepo_graph*`. Done: every key in the releasables.toml sample is decoded into a typed struct; unknown keys are refused at every level; tag formats are always explicit; standalone versus workspace is derived from the member count; the root-member rules of the Python loader are refusals here.

**S4. Options and changelog.** `internal/options` with its generator, `internal/changelog`, the changelog-entry schema at `.strictspec/changelog-entry.schema.toml` (format version 2, `batch_reason`). Ports from: `options.py`, `options_registry.py`, `changelog/*.py`, `prepush_utils.py`, the logic of `commands/changelog_cmd.py`, and `tests/test_options*`, `tests/test_changelog*`, `tests/test_remap*`, `tests/test_prepush*`. Done: the roll-up heading fix has a red test; coverage, exemptions, and range read the nearest release through `internal/releaserecord`.

**S5. Release record and transition record.** `internal/releaserecord` with `.strictspec/release-file.schema.toml` (format version 2), `.strictspec/transition-record-event.schema.toml` (surgery events, `event` discriminator), and the reconcile plan schema. Ports from: `release_file.py`, `release_record.py`, `transition_record.py`, `transition_record_followup.py`, `tag_explanation.py`, `release_commit_remap.py`, and `tests/test_release_file*`, `tests/test_release_record*`, `tests/test_transition_record*`, `tests/test_tag_explanation*`. Done: the four read errors exist with their messages; tag explanation consults the lifecycle library's `TagOwner` and unversioned tags.

**S6. The lifecycle-and-license library (in strictspec).** In `~/Projects/stricttools/tools/strictspec/go`: `strictspec/builtin/lifecycle-and-license.schema.toml`, package `lifecycle` and `lifecycle/index`, tests for every rule, every validation, the append-only check, the index matching, and upserts. Ports from: `private_repo_publishing.py` and `tests/test_private_repo_publishing*`. Done: every rule in the rules table is a constant with an evaluation function and a test per outcome; the package imports nothing from rlsbl.

**S7. Targets, pipelines, Go modules.** `internal/targets` (with the support-matrix generator and the private-paths CI program generator), `internal/pipelines`, `internal/gomodule`. Ports from: `targets/{base,protocol,go,npm,pypi,refs,outcomes,utils,introspect,__init__}.py`, `testing.py`, `upload_exclusions.py`, `nested_exclusions.py`, `private_paths.py`, `pipelines/*.py` minus `build.py` and `cloudflare_pages.py`, `npm_wrapper.py`, `go_*.py`, `module_paths.py`, `ldflags_symbols.py`, and their tests (`tests/test_targets*`, `tests/test_target_*`, `tests/test_pipelines*`, `tests/test_npm_wrapper*`, `tests/test_go_*`, `tests/test_ldflags*`, `tests/test_upload*`, `tests/test_testing*`). Done: `package.json` version writes preserve formatting and refuse anything but one top-level `version`; the npm platform table is the one source for npm and wheel platforms.

**S8. Dependencies and rewrites.** `internal/dependencies`, `internal/rewrite`; commands `rewrite go-module-path`, `rewrite project-name`, `rewrite uv-path-sources`. Ports from: `uv_workspace.py`, `dep_rewrite.py`, `dep_floors.py`, `dep_locks.py`, `strictspec_floor.py`, `commands/rewrite/*.py`, and `tests/test_dep_floors*`, `tests/test_dep_locks*`, `tests/test_dep_rewrite*`, `tests/test_uv_*`, `tests/test_rewrite*`, `tests/test_strictspec_generated_format.py`. Done: `rewrite go-module-path` uses `go/parser`; `project-name` writes identity periods through the library; uv-path-sources asks PyPI only for the package document.

**S9. Checks, part one.** `internal/checks` framework wiring, `checks.toml`, and the families: project consistency, changelog, prepush, release-tagged, lifecycle (`lifecycle-record-valid`, `confidential-names`, `repository-visibility`), external checks, `strictcode`. Ports from: `checks/{__init__,_common,scope,project,changelog,prepush,release}.py`, `external_checks.py`, and their tests (`tests/test_check*`, `tests/test_external_checks*`, and every other file under `tests/` that names one of this slice's checks). Done: the checks registry holds every check this plan keeps or adds and no dropped one (`library-lint`, `dead-workspace-packages`, and `ruff-lint` absent); the `rlsbl:strictcode` option's default is `error`.

**S10. Checks, part two.** Workspace, nested-member, Go tag and workspace, upload, test-runner floor, dev-overlay drift, support-matrix freshness, strictspec generated format, ldflags, toolchain, dep floors and locks wiring. Ports from: `checks/{workspace,nested,go_tags,go_workspace,upload_private_paths}.py`, the check half of `test_sandbox.py`, and their tests (`tests/test_nested*`, `tests/test_go_tag_path_checks.py`, `tests/test_go_companion_tags.py`, `tests/test_go_workspace*`, `tests/test_test_sandbox*`, `tests/test_testisolation_floor.py`, and every other file under `tests/` that names one of this slice's checks). Done: `releasable-residue` reports every old-layout directory.

**S11. Scaffold.** `internal/scaffold`, the templates, `scaffold` command, git hook installation, the version-agreement refusal. Ports from: `commands/init_cmd.py`, `templates/`, `hook_hashes.py`, `action_versions.py`, `ci_yaml.py`, `node_matrix.py`, `scratch_dirs.py`, the rendering half of `test_sandbox.py`, and their tests (`tests/test_scaffold*`, `tests/test_hook*`, `tests/test_action_versions*`, `tests/test_node_matrix*`, `tests/test_scratch*`, including every case of `tests/test_scaffold_version_agreement.py`). Done: the dropped templates are absent; generated publish workflows for a confidential repository carry no build attestation (npm `--provenance`, PyPI attestations), proxy notification, or repository URL; LICENSE writing reads the record.

**S12. Workflows and CI.** `internal/workflows`, `internal/ci`; commands `monorepo sync` (handler), `watch`. Ports from: `commands/monorepo/sync.py`, `publish_inline.py`, `ci_router.py`, `router_filters.py`, `publish_gate.py`, `ci_checks.py`, `publish_workflows.py`, `commands/watch.py`, and their tests (`tests/test_sync*`, `tests/test_publish_inline*`, `tests/test_router*`, `tests/test_ci_*`, `tests/test_watch*`, `tests/test_publish_workflows*`). Done: the npm platform-package and wheel jobs exist with their tests; no generated job name uses a word the house style bans.

**S13. Release, part one.** `internal/publishrules` and the validation half of `internal/release`: lock, release checkout, run state, validation (every Python pre-mutation guard plus the lifecycle and publishing rules), the plan with its entries, hooks (declarations only), version writes, lockfile sync, build, secret scan, packed-artifact contents. Ports from: `commands/release/{validate,phase_a,hooks,release_state,shared,in_checkout}.py`, `release_checkout.py`, `lock.py`, `secret_scan.py`, `strictcli_detect.py`, and their tests (`tests/test_release_validate*`, `tests/test_release_phase_a_seam.py`, `tests/test_release_checkout*`, `tests/test_hooks*`, `tests/test_secret_scan*`, `tests/test_lock*`, `tests/test_main_as_candidate*`). Done: `release.preflightSelection` runs every preflight check whether or not a pre-release hook is declared, and a declared pre-release hook replaces only the built-in tests (tests for both cases); `todo/preflight-checks-skipped-by-custom-hooks.md` moves to `todo/.done/` with a resolution section stating that its first option (always run the built-in preflight checks) was built.

**S14. Release, part two.** The steps from `committed` to `post-release-hooks-run`, deploy, resume, `release run`, `release resume`, `release init`, `status`, `unreleased`, `targets`, `commit`. Ports from: `commands/release/{__init__,execute,steps,publish}.py`, `commands/release_init.py`, `commands/status.py`, `commands/unreleased.py`, `commands/targets_cmd.py`, `commands/commit_cmd.py`, and their tests (`tests/test_release_*` not taken by S13, `tests/test_status*`, `tests/test_unreleased*`, `tests/test_commit*`). Done: the step table holds the steps this plan names and no other; a failing deploy leaves a resumable state and resume reruns it (integration test).

**S15. Operations on past releases.** `internal/releaseops` and `internal/releasenotes` (S14 uses `releasenotes` and writes it first if S15 has not; both slices name the file `internal/releasenotes/compose.go`); commands `release edit`, `retry`, `undo`, `abandon`, `deprecate`, `yank`. Ports from: `commands/{edit_release,release_retry,undo,release_abandon,deprecate,yank}.py`, `evidence_gate.py`, `publication_probe.py`, `release_publication.py`, and their tests. Done: undo evidence makes no version-specific registry request (test).

**S16. History rewrites.** `internal/historyrewrite`; commands `release scrub`, `release reconcile`, `release backfill`. Ports from: `commands/release_scrub.py`, `commands/release_reconcile.py`, `release_backfill.py`, `commands/release_backfill.py`, `preview_apply.py`, and their tests (`tests/test_release_scrub*`, `tests/test_release_reconcile*`, `tests/test_backfill*`, `tests/test_preview_apply*`). Done: identities and unversioned tags explain tags in backfill and reconcile; the mirror promotion source is gone.

**S17. Monorepo commands and batch release.** `internal/monorepo` (all but extract and absorb), `internal/batchrelease`. Ports from: `commands/monorepo/{commands,graph,impact,releasable_rename,batch_release,batch_release_init,batch_plan}.py`, `releasable_cleanup.py`, and their tests (`tests/test_monorepo_*` other than extract and absorb, `tests/test_batch*`, `tests/test_releasable_cleanup*`, `tests/test_releasable_rename*`). Done: `graph --json` payload type and test; rename writes identity periods.

**S18. Extract and absorb.** Ports from: `commands/monorepo/{extract,extract_cmd,absorb_cmd}.py` without the promotion engine, and `tests/test_extract*`, `tests/test_absorb*`. Done: both write the new layout on both sides and the transition record's surgery events.

**S19. Remaining command groups.** `internal/devtools`, `internal/upstream`, `internal/secrets`, `internal/lifecycleops`; commands `dev install`, `dev sync`, `dev status`, `upstream adopt-tags`, `secrets sync-npm-token`, `options registry`, `options set`, every `changelog` command, and every `transition` command. Ports from: `commands/{dev,dev_sync,upstream_cmd,changelog_cmd,transition_record_cmd}.py`, `overlay_state.py`, `upstream.py`, `npm_token.py`, `ci_secrets.py`, and their tests. Done: `transition declassify` follows the order in this plan, with the squash ranges computed in one function, `lifecycleops.declassifySquashPlan`, and an integration test against a fake safegit journal (two periods squashed into two commits, public history before and after kept, a squash message scanned clean, a tag inside a period re-pointed and its archive made `unrecoverable`); `transition init-minimal-record` writes the minimal record and refuses in a repository rlsbl manages and where a record exists (tests).

**S20. The migration.** `internal/migration`, `migrate records`, `scripts/list-rlsbl-repositories.sh`, fixture repositories for each old layout (standalone, workspace with releasables, workspace with member configs, repository with transitions, repository with customized hooks, repository with `dead-modules.toml`). Done: every row of the config-key audit has a test; dry run writes nothing (test); the commit lists only rlsbl-owned paths (test with an unrelated dirty file left alone); every converted fixture gets a lifecycle-and-license record (test); a config naming a removed target is refused, and a test performs the hand edit the refusal names and asserts the refusal clears.

**S21. The strictcode port.** In `~/Projects/strictcode`: the table in "The strictcode port", the declarations path change, lessons for every ported case. Done: every rlsbl check the table names has its rule and lessons; `strictcode.toml`'s schema covers the new tables.

**S22. Enforcement in selfdoc and safegit.** In `~/Projects/stricttools/tools/selfdoc`: deploy and blog enforcement and index upserts, with tests. In `~/Projects/stricttools/tools/safegit`: commit-time scanning and index upserts, with tests, including a repository without a record (scanned as public) and a confidential repository decided from its license periods with no network; the squash mode of `safegit scrub`, with tests of its rewrite journal.

**S24. The cgofree tree-sitter modules.** In `~/Projects/stricttools/tools/cgofree` and `~/Projects/strictcode`: the files-only work in "The cgofree tree-sitter modules". Done: the four directories are tracked with the new module paths; no `github.com/cgofree/tree-sitter` import path remains in them, in cgofree's code and `record.toml`, or in strictcode's sources and `go.mod`; the recipe module-path rule has its red-green test.

**S23. Documentation and help text.** Rewrite rlsbl's selfdoc sources under `.strictmetadata/docs/` for the Go behavior (delete `deploy.md`, `dep-validation.md`, `import-scanning.md`, and `layers.md`; rewrite the others, including `_README.md` and `_CLAUDE.md`), update `selfdoc.json` for Go sources, update the docs directives' input paths, and rewrite every command's help text. Done: no page describes a dropped subsystem or an old path; every help text describes the Go behavior.

## After the build

Every item here is a **coordinator step** unless it only reads or writes files.

1. **First run.** Generate every strictspec validator (`strictspec gen` in rlsbl and strictspec), run `gofmt -l`, then each repository's suite alone: strictspec/go, cgofree (its four tree-sitter modules included), strictcode, selfdoc, safegit, rlsbl (`scripts/go-suite.sh`). Fix every failure red-green. Regenerate the help document, the CLI test-coverage manifest, the options registry, the support matrix, and strictcode's registry dump. Measure the suite's memory and time; a resource problem is fixed at its source before continuing (`~/Projects/CLAUDE.md`).
2. **Migration.** Run `scripts/list-rlsbl-repositories.sh --dry-run`, then for each repository `rlsbl migrate records --dry-run` with the source-built binary, review every plan, collect the license declarations the dry runs ask for from the owner, then apply repository by repository. Each apply is one commit through safegit.
3. **Install** the source-built Go rlsbl, safegit, selfdoc, and strictcode on the machine, so the pre-push and post-rewrite hooks every repository runs reach the Go rlsbl.
4. **Releases of the dependencies**, in the release order below, each with the source-built Go rlsbl, after `rlsbl scaffold` regenerates its workflows. After each library release the dependents' `require` lines are raised.
   - strictcli (its Go releasable), then strictspec.
   - The cgofree tree-sitter modules. First the coordinator confirms that the cgofree checkout has an `origin` at `github.com/stricttools/cgofree` and that GitHub reports the repository public; anything else stops here and goes to the owner. Then cgofree's rlsbl declarations and lifecycle-and-license record are written and committed (see "The cgofree tree-sitter modules"), `generated/tree-sitter` is released, the grammar modules' `require` of the runtime is raised from the placeholder to that released version and committed, and the three grammar modules are released as one batch. Then strictcode's `go.mod` requires the four released versions and its local `go.work` lines for them are deleted.
   - strictcode, selfdoc, and safegit.
   - saferm: its hand-written npm postinstall package is replaced by npm platform packages, per the ruling, under platform package names the owner has confirmed.
5. **Switchover commit in rlsbl.** Raise rlsbl's `require` lines to the released versions and remove the local `use` lines from `go.work`; then one commit deletes the Python (`rlsbl/`, `tests/`, `uv.lock`, the Python maintenance scripts under `scripts/`, `bin/cli.js`, the Python build settings in `pyproject.toml`), deletes `internal/migration` and `migrate records`, and switches rlsbl's own declarations to the go `binary`, npm `go-binary`, and pypi `go-binary` pipelines. Scaffold regeneration of rlsbl's workflows and test runner is committed before it.
6. **Release rlsbl** 0.132.0 with a binary built from the switchover commit (`release run --watch --approve-consequential` on the owner's authorization). The last Python version stays 0.131.0.
7. **Guides.** Rewrite `~/Projects/CONTEXT/rlsbl-reference.md`, `rlsbl-release.md`, `rlsbl-monorepo.md`, `rlsbl-changelog.md`, and `rlsbl-history-rewrite.md` for the Go commands and layout.
8. **Rulings carried out after the release:** browserbuddy is classified (`transition classify` after the owner makes its GitHub repository private), its published npm and PyPI versions are deprecated and yanked with a neutral notice that says nothing about it going private or proprietary, its registry names stay held, and its listing on the family site is removed.

### Release order

strictcli, strictspec, the cgofree tree-sitter modules, strictcode, selfdoc, safegit, saferm, rlsbl. strictspec is in the order because the lifecycle-and-license library is part of its Go module; selfdoc and safegit link it. The cgofree modules come before strictcode, which requires them; within them the runtime comes before the grammars. saferm comes after safegit and before rlsbl, and is the first release with npm platform packages.

## Open points

No point awaits a ruling: the rulings on the rewrite plan at the end of `todo/go-rewrite-decisions.md` are carried out in the sections above.
