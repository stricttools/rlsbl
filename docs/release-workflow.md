---
description: "Reference for the rlsbl release flow: the untagged candidate and its CI gate, bump types, the pre-release channel, pipeline order, hooks, and recovery."
---

# Release workflow

## Overview

`rlsbl release run` orchestrates the full release lifecycle: validates the project state, bumps the version, runs quality checks, commits, **pushes the version-bump commit to the release branch untagged and waits for the repository's own CI to conclude on it**, and only then finalizes the changelog, tags that CI-verified commit, pushes, creates a GitHub Release, and publishes. The entire flow is driven by a release file (`.rlsbl/releases/unreleased.toml`) that declares the bump type, description, and optional context.

This is **main-as-candidate ordering**, and it is the load-bearing property of the whole flow: the tag, the GitHub Release, the finalized changelog and every registry push happen strictly *after* a green CI verdict on the exact commit being released. A red (or unresolved) verdict leaves nothing behind but the candidate commit on the branch — no tag, no GitHub Release, no finalized changelog, nothing on any registry. The version number is therefore never burnt by a failure: the fix lands forward on the release branch at the *same* version and `rlsbl release resume` completes it.

Validation failures abort with no partial state left behind. Once the mutating phase starts, every step records a success or failure marker in an in-progress state file: a fatal failure preserves the state so `rlsbl release resume` can continue from where the release stopped, and non-fatal failures are recorded and loudly named in the completion summary while the release completes (see "Release state and resume" below). `rlsbl release undo` exists for a release that *completed* and turned out to be bad — not for a CI failure, which under this ordering never produces anything to undo.

## Prerequisites

Before running `rlsbl release run`, the project must satisfy several preconditions. Each is enforced as a hard error at the start of the release flow — the release aborts immediately with a clear message indicating which requirement failed and how to fix it. Addressing these upfront avoids partial releases that need manual cleanup.

| Requirement | How to verify | What happens if missing |
| --- | --- | --- |
| Clean working tree | `git status --porcelain` is empty | Hard error (use `--allow-dirty` to override) |
| `gh` CLI authenticated | `gh auth status` | Hard error |
| Changelog coverage | `rlsbl check --tag changelog` passes | Hard error during validation step |
| Release file exists | `.rlsbl/releases/unreleased.toml` present | Hard error (run `rlsbl release init`) |
| Description set | `description` field in unreleased.toml is non-empty | Hard error |

## The release file

The release file at `.rlsbl/releases/unreleased.toml` drives the entire release flow. Scaffold it with `rlsbl release init`, which auto-detects targets, sets a default bump type of `patch`, and generates a template with placeholder fields for description and context:

```toml
# .rlsbl/releases/unreleased.toml
bump = "patch"
description = "Short summary of what this release contains"
context = """
Optional multiline explanation of why these changes were made.
Appears as a collapsible details block in CHANGELOG.md.
"""

[include]
targets = ["pypi", "npm"]
```

### Bump types

| Bump | When to use | Version example |
| --- | --- | --- |
| `patch` | Bug fixes, small improvements, no API changes | 0.5.2 -> 0.5.3 |
| `minor` | New features, backward-compatible additions | 0.5.2 -> 0.6.0 |
| `major` | Breaking changes, API removals, incompatible changes | 0.5.2 -> 1.0.0 |
| `infra` | Infrastructure-only releases with zero user-facing entries | 0.5.2 -> 0.5.3 |
| `prerelease` | Advance an existing pre-release: next counter, or promote to the next channel | 0.6.0-alpha.0 -> 0.6.0-alpha.1 |

For pre-stable projects (0.x.x), breaking changes are a minor bump. Never bump to 1.0.0 without explicit authorization.

`infra` exempts the release from the at-least-one-user-facing-entry gate and **forbids** user-facing entries. It is not a hotfix mechanism — a user-facing hotfix is a `patch`.

### Description and context

- **description** (mandatory): A short summary of the release. Appears as a paragraph under the version heading in CHANGELOG.md and as the GitHub Release title suffix.
- **context** (optional): Multiline explanation of design decisions, rename rationale, or migration notes. Renders as a collapsible `<details>` block in CHANGELOG.md.

### Per-target configuration sections

Some targets require additional configuration in the release file via `[targets.<name>]` sections, providing target-specific metadata that cannot be inferred from the project's manifest. Currently, this applies only to the Flutter target, which needs a deployment mode declaration to distinguish OTA updates from full app store builds.

**Flutter target** requires a `[targets.flutter]` section with a `mode` field. Valid modes:

| Mode | Description |
| --- | --- |
| `ota` | Over-the-air update (code push without a full app store rebuild) |
| `build` | Full build release (triggers app store build pipeline) |

If a Flutter target is listed in `include` but has no corresponding `[targets.flutter]` section with a `mode` field, the release file validation fails with a hard error.

Example with Flutter per-target config:

```toml
# .rlsbl/releases/unreleased.toml
bump = "minor"
description = "Add offline sync support"
include = ["flutter"]
exclude = []

[targets.flutter]
mode = "build"
```

`rlsbl release init` auto-generates the `[targets.flutter]` section with `mode = "build"` as the default when a Flutter target is detected. Change the mode before running `rlsbl release run` if an OTA release is intended.

Target config sections for targets not listed in `include` are rejected as validation errors. Only fields documented for a target type are allowed — unknown fields cause a hard error.

## The pre-release channel

Pre-releases are a first-class release channel, not a workaround: a version can ship to real consumers as `0.6.0-alpha.0` and be promoted through `beta` and `rc` to the stable `0.6.0` without the version number ever burning or the release flow changing shape. Every release step — validation, changelog finalization, tagging, the GitHub Release, publishing — runs exactly as it does for a stable release.

### The identifiers

`preid` selects the channel: `alpha`, `beta`, `rc`, or `stable`. They are **ordered** — `alpha < beta < rc < stable` — and the ordering is enforced:

| Situation | Result |
| --- | --- |
| Advance within a channel | `0.6.0-beta.1` -> `0.6.0-beta.2` |
| Promote to a later channel | `0.6.0-alpha.3` -> `0.6.0-beta.0` (counter restarts at 0) |
| Promote to `stable` | `0.6.0-rc.2` -> `0.6.0` (suffix stripped) |
| Demote to an earlier channel | Hard error — `Cannot demote pre-release from "beta" to "alpha"` |
| Any preid with `bump = "infra"` | Hard error — infra releases cannot be pre-releases |
| `preid = "stable"` with a bump other than `prerelease` | Hard error — stabilizing is an operation on an existing pre-release |

An unknown identifier is a hard error listing the valid four. `preid = ""` (and whitespace) means *unset*, not "some default channel" — there is no implicit pre-release.

### Entering, advancing, and leaving the channel

**Enter** with a normal bump plus a `preid`. The base version bumps as usual and gains a `-<preid>.0` suffix:

```toml
# .rlsbl/releases/unreleased.toml -- 0.5.2 becomes 0.6.0-alpha.0
bump = "minor"
preid = "alpha"
description = "First alpha of the new resolver"
```

**Advance** with `bump = "prerelease"`. Omitting `preid` (or repeating the current one) increments the counter; naming a later identifier promotes and restarts the counter at 0:

```toml
# 0.6.0-alpha.0 -> 0.6.0-alpha.1
bump = "prerelease"
description = "Second alpha: resolver fixes"
```

```toml
# 0.6.0-alpha.1 -> 0.6.0-beta.0
bump = "prerelease"
preid = "beta"
description = "Beta: resolver API frozen"
```

**Leave** the channel with `preid = "stable"`, which strips the suffix and ships the base version that was reserved all along:

```toml
# 0.6.0-rc.2 -> 0.6.0
bump = "prerelease"
preid = "stable"
description = "0.6.0 stable"
```

`bump = "prerelease"` on a version with no pre-release suffix is a hard error — there is nothing to advance. Enter the channel with a normal bump first.

### What a pre-release does differently downstream

Only the distribution side changes, and it changes automatically from the version string:

| Surface | Pre-release behavior |
| --- | --- |
| GitHub Release | Marked as a **pre-release** (any version containing `-`). |
| npm / pnpm / yarn publish | Published under the `--tag <preid>` dist-tag, so `npm install <pkg>` still resolves the latest stable. |
| Changelog | Finalized to `x.y.z-preid.N.jsonl` and sorted before the matching stable version. |
| Tags | Same scheme as stable (`v0.6.0-alpha.0`, or `<name>@v0.6.0-alpha.0` in a monorepo). |

### Declaring it

The release file's `preid` key is the normal path, and `rlsbl release init` scaffolds it as a commented line. The `--preid` flag on `rlsbl release run` is valid **only** alongside `--bump` (the flag pair that bypasses the release file entirely). In a monorepo, each `[releasables.<name>]` / `[packages.<name>]` section carries its own `preid`, so one workspace release can ship some packages stable and others as alphas.

## Release pipeline order

The release pipeline executes its steps in a fixed order, from initial validation through post-release hooks. Validation steps abort with no partial state left behind; once the mutating phase starts, progress is tracked in an in-progress state file so a failed release can be resumed with `rlsbl release resume`. Steps 9 and 10 are conditionally skipped when the pre-release hook is customized.

Steps 15 and 16 are the **candidate push and the CI gate**: everything above them is reversible, everything below them is not. The gate is the dividing line of the whole flow.

| Step | Action | Abort on failure |
| --- | --- | --- |
| 1 | Verify `gh` auth and clean working tree | Yes |
| 2 | Read `unreleased.toml` for bump type, description, context, and target selection | Yes |
| 3 | Validate JSONL changelog (all 9 checks) | Yes |
| 4 | Generate CHANGELOG.md from all JSONL files | Yes |
| 5 | Run `pre-checks.sh` hook | Yes |
| 6 | Run strictcli schema dump (`--dump-schema`) if project uses strictcli | Yes |
| 7 | Run `selfdoc gen --no-auto-commit` if project uses selfdoc | Yes |
| 8 | Run selfdoc check (verify generated files are up-to-date) if project uses selfdoc | Yes |
| 9 | Run built-in tests (`uv run pytest` / `go test` / `npm test`) | Yes |
| 10 | Run built-in lint (library projects only) | Yes |
| 11 | Run `pre-release.sh` hook | Yes |
| 12 | Write new version to all detected target files + `.rlsbl/version` | Yes |
| 13 | Commit (message = tag string, e.g. `v1.2.3`) — **not** tagged | Yes |
| 14 | Regenerate the monorepo snapshot, so the snapshot commit is part of what CI verifies | Yes |
| 15 | **Push the version-bump commit to the release branch UNTAGGED** — this is the release candidate | Yes |
| 16 | **CI gate**: wait in-process for the repository's own push-triggered CI to conclude on that exact commit | Yes (nothing is tagged, finalized or published while it is not green) |
| 17 | Finalize JSONL: rename `unreleased.jsonl` to `x.y.z.jsonl` (chmod 444), create fresh `unreleased.jsonl`, regenerate CHANGELOG.md, generate `x.y.z.md`, commit | Yes (state preserved, resumable) |
| 18 | Archive the release file to `v{version}.toml` and regenerate `x.y.z.md` from the archived metadata | Yes (state preserved, resumable) |
| 19 | Tag the **CI-verified commit** (plus Go companion tags in releasable mode) | Yes (state preserved, resumable) |
| 20 | Push the finalization commits and the tags | Yes (state preserved, resumable) |
| 21 | Create GitHub Release with the version's changelog section as notes | Yes (state preserved, resumable) |
| 22 | Upload assets if pipeline has `assets` or `custom_assets` configured | Yes (state preserved, resumable) |
| 23 | Run pipeline `publish` for each configured pipeline (skipped for `publish_mode: "none"`) | Yes (state preserved, resumable) |
| 24 | Deploy configured targets | No (failure recorded and named in the completion summary) |
| 25 | Run `post-release.sh` hook | No (failure recorded and named in the completion summary) |
| 26 | Regenerate the monorepo snapshot post-hoc, if the pre-push slot at step 14 was forfeit | No (failure recorded and named in the completion summary) |
| 27 | Print `Watch CI: rlsbl watch <sha>` | -- |

The tag at step 19 is placed on the commit CI verified at step 16, **not** on HEAD: the finalization commits from steps 17-18 land on top of it and are pushed alongside it at step 20. This is what makes "the tag points at a CI-green tree" true rather than approximately true.

### The CI gate

The gate blocks the irreversible half of the release until the repository's own CI has spoken about the candidate commit, and it distinguishes four outcomes rather than collapsing them into pass/fail. The distinction matters because the right operator response differs sharply between a definite failure, an unfinished wait, and a repository that simply has no CI to wait for:

| Verdict | What it means | What the release does |
| --- | --- | --- |
| Green | Every push-triggered run for the candidate concluded successfully | Proceeds to finalization |
| Red | At least one run definitively failed | Hard error with fix-forward guidance; nothing tagged, finalized or published |
| Timeout | The wait ran out with runs still unresolved | Hard error saying so honestly — the runs may still be in flight, so the remedy is to check them (`rlsbl watch <sha>`) and resume, not to go fix code that may be fine |
| Not configured | The repository declares no push-triggered workflow at all | Proceeds **without** a gate, and says so loudly on stderr (the notice is unconditional — `--quiet` cannot suppress it) |

Push-triggered CI that produces no runs at all within the discovery grace is a hard error, never a silent proceed. The whole wait is bounded by `--ci-timeout` (config key `ci_timeout`, default 3600s); run discovery is spent *inside* that budget and is capped at half of it, so a short budget always leaves the runs a real window to complete in.

Between steps 2 and 3, four pre-mutation guards run unconditionally -- they are direct validation calls, not preflight-tag checks, so a customized pre-release hook never skips them:

- **Range pin** -- HEAD is pinned before *any* mutation (including the pre-mutating selfdoc auto-commit), and every commit the release itself creates is recorded in a trail on the state file. The pin range is re-checked at four checkpoints: the mutating entry, the candidate push, immediately after the CI gate, and the final push. A commit in the range that the release did not create -- a concurrent session sharing the worktree, an editor auto-commit, a hook -- is a hard error naming every foreign SHA with its subject. Nothing is rolled back: the guard refuses to *ship* foreign work, never to destroy it. The batch orchestrator takes the same pin at the workspace level and checks it at the batch CI gate.
- **Scaffold conflict guard** -- unresolved merge conflict markers in scaffold-managed files abort the release.
- **Cross-repo path source guard** -- a committed `pyproject.toml` (including releasable member packages) declaring a `[tool.uv.sources]` path entry that resolves outside the repository aborts the release. See the `cross-repo-path-sources` check in [checks](checks.md).
- **Version-skew guard** -- if `dev-sources.toml.local-only` declares local checkout overlays (see [dev workflow](dev-workflow.md)), each overlaid package's local version is compared against its latest PyPI release. Local ahead of the registry aborts with "release the dependency first: `<pkg>` local X > registry Y" -- the release was developed against unreleased dependency code. An unpublished overlay package or a registry/network failure is also a hard error, never a silent skip. No overlays file means nothing to check.

Steps 9 and 10 are conditionally skipped — see the hooks override mechanism below.

### Release state and resume

From the version bump onward, every step records a success or failure marker in an in-progress state file (`.rlsbl/releases/in-progress.json`; for releasable releases, `.rlsbl-monorepo/releasables/<name>/releases/in-progress.json`). If a fatal step fails (anything through pipeline publish), the state file is preserved and `rlsbl release resume` continues from where the release stopped, skipping already-completed steps including post-release steps such as asset upload.

Non-fatal failures (deploy, post-release hook, snapshot) are recorded and loudly named in the completion summary, and the release completes. The state file is cleared only when every step carries a marker and no fatal step failed; `rlsbl release run` auto-clears a provably-complete leftover state file instead of blocking.

## Publish gating

Scaffolded publish workflows trigger on `release: published` and `workflow_dispatch`, which means they used to race CI on the same commit -- a broken artifact could publish before CI reported. Every scaffolded publish workflow (all targets, merged multi-target workflows, and the monorepo publish router) now begins with a `gate` job, and every publish job depends on it (`needs: gate`). No artifact is built or published until the gate passes.

The gate resolves the release commit ref-based: it uses the workflow run's own `GITHUB_SHA`, which is the tag's commit both for release-triggered runs and for `workflow_dispatch` runs at the tag ref. It never reads the release event payload (dispatch retries have none). It then polls the GitHub checks API (`repos/{owner}/{repo}/commits/{sha}/check-runs`) until this project's CI check runs -- matched by job name via the `CI_CHECK_REGEX` job env -- complete. The gate's own workflow run is excluded from the poll so it cannot deadlock on itself.

### Conclusion semantics

| CI check conclusion | Gate behavior |
| --- | --- |
| `success` (all matching checks) | Gate passes; publish jobs run |
| `failure` / `timed_out` | Hard error: CI did not pass on the release commit |
| `cancelled` | Hard error with explanation -- a cancelled run proves nothing about the commit; the gate never waits for a conclusion that will never come |
| `skipped` | Hard error with explanation -- the project's own CI must actually run on the release commit |
| No matching check runs after a grace window (default 5 minutes) | Hard error -- a scaffolded repository always has CI, so the release commit must produce check runs |
| Checks still running past the timeout (default 20 minutes) | Hard error listing each pending check |

Timeout, grace window, and poll interval are job env values (`GATE_TIMEOUT_MINUTES`, `GATE_GRACE_MINUTES`, `GATE_POLL_SECONDS`) -- edit them in the generated workflow if a repository's CI needs different limits. The gate job carries its own `permissions: checks: read`.

### Retry contract

Under main-as-candidate ordering a tag only ever exists on a commit whose CI already went green, so the gate is a safety net rather than a routine obstacle. When it does block a dispatch, the remedy depends on *why*:

- **CI is red on the tagged commit.** Do not re-run CI on that commit expecting a different answer — a failure baked into the code fails identically every time. Fix forward on the release branch and cut the next release; if the tagged version is already public and broken, `rlsbl release deprecate` or `rlsbl release yank` it.
- **The publish run itself failed** (a registry hiccup, an expired token) while CI on the tagged commit is green. Dispatch the publish workflow **at the tag ref** rather than a branch ref, so the gate and all version reads resolve to the tagged release commit:

```bash
gh workflow run publish.yml --ref <tag>
```

Because the gate, all job conditions, and all version reads are ref-based, a dispatch at the tag ref behaves identically to the original release-triggered run. `rlsbl release retry` and the watch auto-retry already dispatch at the tag ref. A bare dispatch from a branch gates on that branch head's CI instead (standalone repos) or hard-errors (monorepo router, where the ref selects the releasing project).

### Monorepo router

The generated publish router emits 1 shared gate job. Member gate jobs are stripped during inlining and every inlined job is rewired to the shared gate. The gate resolves the releasing project from the tag ref prefix (the same prefix used in job `if:` conditions, which match `github.ref_name`) and waits only for that project's CI check runs.

Sibling projects' paths-filtered (skipped) CI checks are outside the filter and never block a release. CI check runs are named `<router job key> / <ci job name>` because the CI router inlines each member's CI jobs and gives every inlined job that explicit `name:` -- the naming the reusable-workflow era produced, kept deliberately so these regexes and any branch protection rules keep matching.

#### The releasable run-everything hook

In explicit releasable mode, the CI router's paths filter for **every** member of a releasable ends with one shared extra entry: the releasable's own `CHANGELOG.md` under `.rlsbl-monorepo/releasables/<name>/`. This is deliberate, and it is what makes a releasable release gateable at all.

A release commit can touch nothing under a member's own directory. That is guaranteed on a **first** release, where the version write is a no-op, and possible on any release whose per-member writes all land elsewhere. Without the shared entry, that member's CI job concludes `skipped` on the exact commit its tag points at -- and the gate refuses a skipped check, correctly, because a skipped check proves nothing about the commit. There is no recovery from that state either: re-running CI on the commit skips the job again, for the same reason it skipped the first time. The release commit always regenerates and commits the releasable `CHANGELOG.md` (it gains the new version's heading), so anchoring every member's filter on that one path makes the gated commit verifiable for all members.

The cost is real and accepted: **releasing a releasable runs the full CI job set of every member of that releasable**, including members whose own code did not change. CI minutes are the price of never tagging a commit the gate cannot read a verdict for. The gate is not relaxed to accept `skipped` -- that would let a release publish on a commit nothing actually verified.

The same filter has a visible consequence for ordinary (non-release) pushes: a push whose diff touches only paths outside every member's filter -- a dev node project's own directory, say, or a root file no member watches -- leaves every member's CI job `skipped` on that commit. For a push this is correct: nothing a member ships changed. A **release** never lands in that state, because the release commit always touches the releasable `CHANGELOG.md`. If you need a member's CI to run on a commit that changed nothing of the member's, make the commit touch something that member's filter matches -- do not loosen the gate.

Only the finalize artifact is in the filter, never the whole releasable directory: `rlsbl changelog add` writes the releasable's JSONL between releases, and those entries must not spend every member's CI minutes.

### Publish concurrency

Publish workflows carry a per-ref concurrency group with `cancel-in-progress: false`: a dispatch retry at the same tag queues behind an in-flight run instead of racing it, and a publish run is never cancelled mid-flight.

## Hooks

Three shell scripts in `.rlsbl/hooks/` provide extension points at different stages of the release pipeline. Each hook runs in the project root directory with the new version available as `$RLSBL_VERSION`. A non-zero exit code from `pre-checks.sh` or `pre-release.sh` aborts the release immediately, while `post-release.sh` failures are logged but do not roll back the already-published release.

| Hook | Runs at step | Ownership | Three-way merged on scaffold | Failure behavior |
| --- | --- | --- | --- | --- |
| `pre-checks.sh` | 5 | User-owned | No (created once, never touched again) | Non-zero aborts release |
| `pre-release.sh` | 11 | Scaffold-managed | Yes | Non-zero aborts release |
| `post-release.sh` | 25 | Scaffold-managed | Yes | Non-fatal (release continues) |

### Hooks override

When `pre-release.sh` has been customized — meaning its content hash does not match any known scaffold template version — steps 9 (built-in tests) and 10 (built-in lint) are skipped entirely. The assumption is that a customized pre-release hook handles testing and linting itself.

The override triggers when:
- The hook file exists AND its content differs from all known template versions (compared by SHA-256 hash with trailing whitespace stripped)

The override does NOT trigger when:
- The hook file is missing
- The hook file matches any known scaffold template version (including historical versions)

This means an unmodified scaffold hook or a missing hook file is considered "effectively empty" — built-in tests and lint run normally.

## Flags

`rlsbl release run` accepts both global flags (shared with all rlsbl commands) and release-specific flags that control working tree validation and post-release CI monitoring. `--watch` is a required negatable boolean — either `--watch` or `--no-watch` must be specified explicitly.

| Flag | Effect |
| --- | --- |
| `--dry-run` | Preview the entire flow without making changes (no commits, tags, pushes, or GitHub Releases) |
| `--approve-consequential` | Skip the confirmation prompt a `consequential` command asks before it runs |
| `--watch` | After release, automatically watch CI runs to completion (blocking, in-process) |
| `--no-watch` | After release, print the watch command hint without watching |
| `--allow-dirty` | Skip the clean working tree check (step 1) |

`--dry-run`, `--approve-consequential`, `--quiet` and `--verbose` are framework-owned flags available on all rlsbl commands. `--allow-dirty` and `--watch` are release-specific. The same `--watch` / `--no-watch` pair applies to `rlsbl release resume`, `rlsbl release retry`, and `rlsbl monorepo release run`.

Watching is always in-process: there is no detached background watcher. To watch later, run `rlsbl watch <sha>` — the hint `--no-watch` prints is exactly that command.

## Related commands

The `release` command group covers the full release lifecycle — from scaffolding the release file through post-release corrections and rollbacks. Each subcommand is designed for a specific phase: `init` prepares, `run` executes, `resume` continues a release that stopped (most often at a red CI gate), `retry` re-dispatches publish workflows, `edit` corrects release notes, `undo` reverts a completed release, and `deprecate` / `yank` retire published ones.

| Command | Purpose |
| --- | --- |
| `rlsbl release init` | Scaffold `.rlsbl/releases/unreleased.toml` with auto-detected targets |
| `rlsbl release resume` | Continue a release that stopped, skipping completed steps. Re-pins at the current tip, so a fix-forward commit is adopted and the same version completes. |
| `rlsbl release retry` | Re-dispatch publish workflows for a completed release (reads from `retry.toml`) |
| `rlsbl release edit [version]` | Sync GitHub Release notes from CHANGELOG.md (defaults to current version) |
| `rlsbl release undo` | Revert a completed release: delete GitHub Release, delete tag, revert commit. Requires manual `git push` after. |
| `rlsbl release deprecate <version>` | Flag a published release as deprecated on GitHub, with an optional reason and replacement |
| `rlsbl release yank <version>` | Registry-aware removal of a published version (npm deprecate, cargo yank, Go retract, PyPI checklist) |
| `rlsbl release scrub` | Scrub sensitive content from history and re-align tags, changelog hashes and GitHub Releases |
| `rlsbl release reconcile` | Re-push tags a history rewrite moved and recreate their GitHub Releases, driven by safegit's rewrite journal |

## Dev node projects

Dev nodes are projects at the edge of the dependency graph that nothing user-facing depends on — test infrastructure, conformance suites, dev tooling, and internal utilities consumed only during development. Dev nodes cannot be released:

- **No changelog system**: no `.rlsbl/changes/`, no `unreleased.jsonl`, no `CHANGELOG.md`
- **No releases**: `rlsbl release run` and `rlsbl release edit` error with "dev_node projects cannot be released"
- `rlsbl changelog add` errors with "dev node projects don't use changelogs"
- Scaffold skips changelog infrastructure
- Pre-push check ignores dev node commits
- Batch release (`rlsbl monorepo release run`) excludes dev nodes
- Remove `dev_node = true` from workspace.toml to make a project releasable
- The `dev-only-boundary` check prevents non-dev-node projects from declaring runtime dependencies on dev nodes

## Scrubbing sensitive content

When sensitive content is discovered in git history (credentials, confidential project names, etc.), `rlsbl release scrub` wraps safegit's history rewriting with automatic release metadata cleanup. Rewriting history changes every commit SHA from the rewrite point forward, which would normally break the JSONL changelog's hash references, the validation cache, and existing GitHub Releases — so the command repairs all of that release metadata in one pass.

Usage:
```
rlsbl release scrub --pattern "secret_token_.*" --replace "REDACTED" --reason "Remove leaked API keys" --entire-history
rlsbl release scrub --file config/secrets.yml --reason "Remove secrets file" --from-commit a1b2c3d
```

The command:
1. Runs safegit scrub (match, file, or recipe mode) with repeatable `--remap-shas-in` globs covering every changelog directory (`.rlsbl/changes/*.jsonl` per project, plus `.rlsbl-monorepo/releasables/*/changes/*.jsonl` in monorepos). safegit remaps the full commit hashes INSIDE the JSONL files at every commit of the rewritten history, so all historical versions of the changelogs — including HEAD — stay self-consistent after the rewrite. The glob list is derived from the same enumeration the validation step uses, so remap coverage and validation coverage cannot diverge. Committed scrub archives (`.rlsbl/scrubs/*.json`) are deliberately excluded: they are records of what WAS, their old-side SHAs dangle by design, and validation never reads them.
2. Verifies safegit's machine-readable cleanup status: `cleanup_ok: false` is a hard error before anything is committed (the hash validation depends on pre-rewrite objects being pruned). On a resumed run after a manual prune, the gate re-checks reality and continues once no pre-rewrite object resolves.
3. Validates that every commit hash in every JSONL changelog file resolves. Nothing is rewritten by rlsbl on this path — the in-history remap already produced consistent worktree content.
4. Recovery fallback: when validation finds dangling hashes AND safegit's persisted rewrite journal (`.git/safegit/rewrite-maps.jsonl`) can fix them, the working-tree JSONL files are repaired from the journal's commit map and re-validated. This covers scrubs that ran outside `rlsbl release scrub` (no `--remap-shas-in`), scrubs interrupted between safegit finishing and rlsbl's steps, and abbreviated hashes (which the in-history remap deliberately skips). A journal group with a `start` record but no `complete` record is a crashed rewrite and is surfaced loudly. The same validation and journal repair also run when the scrub finds nothing to rewrite: damage left behind by a previous crashed or direct scrub is repaired and committed on the spot (no force-push is needed -- nothing was rewritten), instead of surfacing later as unexplained check failures.
5. Regenerates the changelog and asserts the output is byte-identical to what is on disk. A diff is a hard error (something else is wrong — e.g. a hand-edited CHANGELOG.md); the diff is shown and the on-disk originals are restored.
6. Invalidates validation caches and commits the scrub artifacts: the audit archive, tracked `.validated` deletions, and any journal-repaired files. Changelog files are not part of the commit — HEAD is already consistent.
7. Force-pushes the branch and affected tags, each with an explicit `--force-with-lease` expectation captured from the actual remote (`git ls-remote`) before the rewrite. safegit's `pre_rewrite_remotes` (the local tracking snapshot) is only cross-checked informationally — it may be stale and is never the lease authority.
8. Recreates GitHub Releases for affected tags with updated changelog notes

Flags: `--pattern`, `--file`, or `--recipe` (what to scrub), `--replace` or `--mangle` (match-mode replacement strategy; both require `--pattern`), `--reason` (required, appears in commit message), `--from-commit` or `--entire-history` (scope; file mode requires `--from-commit`).

Error recovery: if the command fails partway, `scrub-result.json` preserves the safegit output at `.rlsbl/releases/scrub-result.json` (for releasable releases: `.rlsbl-monorepo/releasables/<name>/releases/scrub-result.json`). Re-running the command resumes from the last completed step without re-running safegit.

A direct `safegit scrub` in an rlsbl-managed repository is not blocked, but it leaves rlsbl's release metadata behind: the JSONL changelogs keep pre-rewrite hashes, the tags still point at pruned commits, and the GitHub Releases go stale. `rlsbl release scrub` does the rewrite and that repair in one pass; after an out-of-band rewrite, `rlsbl release reconcile` repairs the metadata from safegit's rewrite journal.

Requires safegit 0.25.0+, for `--remap-shas-in`, the persisted rewrite journal, and the cleanup_ok/pre_rewrite_remotes JSON fields (0.22.0), and for destructive rewrites that no longer take `--json` as consent, so rlsbl passes `--approve-consequential` explicitly (0.25.0); safegit declares all three scrub modes `consequential`.

## Examples

### Full release from start to finish

This example walks through a complete release session after implementing a new feature and fixing a bug. It covers checking project state, adding changelog entries for uncovered commits, initializing the release file with the desired bump type, and executing the release with CI monitoring. Each step shows the actual commands and their expected output so you can follow along in your own project:

```bash
# 1. Check project state
rlsbl status
#   Package: mylib
#   Version: 0.5.2 (pyproject.toml)
#   Branch:  main
#   Last tag: v0.5.2
#   JSONL:   3/3 commits covered
#   ! 3 commits ahead of v0.5.2

# 2. Add changelog entries for each commit (if not already done)
rlsbl changelog add --commits a1b2c3d --description "Add retry logic to HTTP client" --type feature
rlsbl changelog add --commits e4f5g6h --description "Fix timeout crash on slow connections" --type fix
rlsbl changelog add --commits i7j8k9l --no-user-facing

# 3. Verify changelog coverage
rlsbl check --tag changelog
#   changelog-hashes .............. pass
#   changelog-range ............... pass
#   changelog-coverage ............ pass
#   changelog-schema .............. pass
#   changelog-user-facing ......... pass

# 4. Scaffold the release file
rlsbl release init
#   Created .rlsbl/releases/unreleased.toml

# 5. Edit the release file: set bump type and description
#    bump = "minor"
#    description = "Add retry logic and fix timeout handling"

# 6. Run the release
rlsbl release run --no-allow-dirty --watch --approve-consequential
#   Reading .rlsbl/releases/unreleased.toml ...
#   Bump: minor (0.5.2 -> 0.6.0)
#   Validating JSONL changelog ... OK
#   Generating CHANGELOG.md ... OK
#   Running tests ... OK
#   Writing version 0.6.0 to pyproject.toml ... OK
#   Committing v0.6.0 ... OK
#   Pushed release candidate 9f2a1c4b8e07 to origin/main (untagged)
#   Waiting for CI on the release candidate 9f2a1c4b8e07 ...
#   CI is green on 9f2a1c4b8e07
#   Finalizing JSONL ... OK
#   Tagged: v0.6.0 -> 9f2a1c4b8e07 (CI-verified)
#   Pushing ... OK
#   Creating GitHub Release v0.6.0 ... OK
#   Watching CI ...
```

### Dry run preview

Preview what a release would do without making any changes to the repository, registry, or GitHub. The dry-run flag runs the full validation pipeline (JSONL checks, version consistency, test suite) but stops before writing version files, committing, tagging, pushing, or creating GitHub Releases. Use this to verify that all checks pass and the bump type produces the expected version before executing a real release:

```bash
rlsbl release run --no-allow-dirty --no-watch --approve-consequential --dry-run
#   [DRY RUN] Bump: patch (0.6.0 -> 0.6.1)
#   [DRY RUN] Would write version 0.6.1 to pyproject.toml
#   [DRY RUN] Would commit, push the candidate, gate on CI, tag v0.6.1, and push
#   [DRY RUN] Would create GitHub Release v0.6.1
```

### Recovering from a red CI gate

When CI goes red on the release candidate, there is nothing to undo. The candidate commit is on the release branch, but no tag exists (local or remote), no GitHub Release exists, the changelog is still `unreleased.jsonl`, and nothing reached any registry. The version number is not burnt — fix forward on the release branch and resume the *same* version:

```bash
# 1. Fix the failure and commit it on the release branch
git commit -m "Fix the flaky test"

# 2. Record it, exactly as you would any other commit
rlsbl changelog add --commits f1x2d3e --description "Fix flaky test" --type fix

# 3. Resume: re-pushes the new tip as the candidate, re-runs the CI gate,
#    and completes the SAME version once it is green.
rlsbl release resume
```

Do **not** start a new release at a higher version to escape a red CI, and do not re-run CI on the same commit expecting a different answer: a failure baked into the code fails identically every time. The same recipe applies to a batch (`rlsbl monorepo release run` — each member resumes at its own unchanged version) and to a timeout verdict, except that a timeout means the runs may still be in flight, so check them (`rlsbl watch <sha>`) before deciding there is anything to fix.

> **Check `git log` before resuming.** `rlsbl release resume` deliberately re-pins at the *current* branch tip, because the whole point is to adopt the fix commit you just made. That means it adopts **every** commit landed since the failure, including another session's work sharing the worktree. Those commits ship under this version's changelog. Review the range before resuming, and move anything that does not belong onto a branch of its own.

### Recovering from a release that completed and was wrong

`rlsbl release undo` is for a release that ran to completion and then turned out to be bad — not for a CI failure, which under this ordering leaves nothing to undo. It deletes the GitHub Release, removes the git tag from both local and remote, and reverts the version bump commit; push manually afterwards.

For a version that already reached a public registry, prefer `rlsbl release deprecate` (a soft flag on the GitHub Release) or `rlsbl release yank` (registry-aware removal). An undo cannot unpublish what a registry has already served.

## Source reference

The release workflow is implemented in the `rlsbl.commands.release` module, which orchestrates the full release pipeline (see the step table above) from validation through GitHub Release creation and the post-release phase. This module coordinates version bumping, JSONL finalization, git operations, and hook execution.

:-: ref path="rlsbl.commands.release"
