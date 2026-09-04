---
description: "The rlsbl release flow: the untagged candidate and its CI check, the release commit and what the release record is authoritative over, and the bump types."
---

# Release workflow

## Overview

`rlsbl release run` orchestrates the full release lifecycle: validates the project state, bumps the version, runs quality checks, commits, **pushes the version-bump commit to the release branch untagged and waits for the repository's own CI to conclude on it**, and only then finalizes the changelog, tags that CI-verified commit, pushes, creates a GitHub Release, and publishes. The entire flow is driven by a release file (`.rlsbl/releases/unreleased.toml`) that declares the bump type, description, and optional context.

This is **main-as-candidate ordering**, and it is the property the whole flow rests on: the tag, the GitHub Release, the finalized changelog and every registry push happen strictly *after* a green CI verdict on the exact commit being released. A red (or unresolved) verdict leaves nothing behind but the candidate commit on the branch — no tag, no GitHub Release, no finalized changelog, nothing on any registry. The version number is therefore never burnt by a failure: the fix is committed forward on the release branch at the *same* version and `rlsbl release resume` completes it.

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

Both survive the release: at step 18 the file is archived as `.rlsbl/releases/v{version}.toml` (read-only), and every later changelog regeneration reads the description and context back out of that archive. The archive also gains the two release commit fields the flow writes there — see [the release commit](#the-release-commit). Those two are the flow's alone; writing either into `unreleased.toml` by hand aborts the release.

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

The release file's `preid` key is the only way to declare it, and `rlsbl release init` scaffolds it as a commented line. In a monorepo, each `[releasables.<name>]` section carries its own `preid`, so one workspace release can ship some releasables stable and others as alphas.

## Release pipeline order

The release pipeline executes its steps in a fixed order, from initial validation through post-release hooks. Validation steps abort with no partial state left behind; once the mutating phase starts, progress is tracked in an in-progress state file so a failed release can be resumed with `rlsbl release resume`. Steps 9 and 10 are conditionally skipped when the pre-release hook is customized.

Steps 15 and 16 are the **candidate push and the CI gate**: everything above them is reversible, everything below them is not. The gate is the dividing line of the whole flow.

| Step | Action | Abort on failure |
| --- | --- | --- |
| 1 | Verify `gh` auth and clean working tree | Yes |
| 2 | Read `unreleased.toml` for bump type, description, context, and target selection | Yes |
| 3 | Validate JSONL changelog (every structural check) | Yes |
| 4 | Generate CHANGELOG.md from all JSONL files | Yes |
| 5 | Run `pre-checks.sh` hook | Yes |
| 6 | Run strictcli schema dump (`--dump-schema`) if project uses strictcli | Yes |
| 7 | Run `selfdoc gen --no-auto-commit` if project uses selfdoc | Yes |
| 8 | Run selfdoc check (verify generated files are up-to-date) if project uses selfdoc | Yes |
| 9 | Run built-in tests (`uv run pytest` / `go test` / `npm test`) | Yes |
| 10 | Run built-in lint (library projects only) | Yes |
| 11 | Run `pre-release.sh` hook | Yes |
| 12 | Write new version to all detected target files + `.rlsbl/version`, and re-sync the lockfiles that write stales (including a non-releasable workspace project whose `uv.lock` records a bumped sibling as an editable path source) | Yes |
| 13 | Commit (message = tag string, e.g. `v1.2.3`) — **not** tagged | Yes |
| 14 | Regenerate the monorepo snapshot, so the snapshot commit is part of what CI verifies | Yes |
| 15 | **Push the version-bump commit to the release branch UNTAGGED** — this is the release candidate | Yes |
| 16 | **CI gate**: wait in-process for the repository's own push-triggered CI to conclude on that exact commit | Yes (nothing is tagged, finalized or published while it is not green) |
| 17 | Finalize JSONL: rename `unreleased.jsonl` to `x.y.z.jsonl` (chmod 444), create fresh `unreleased.jsonl`, regenerate CHANGELOG.md, generate `x.y.z.md`, commit | Yes (state preserved, resumable) |
| 18 | Archive the release file to `v{version}.toml`, **recorded at the CI-verified commit and the released trees**, and regenerate `x.y.z.md` from the archived metadata | Yes (state preserved, resumable) |
| 19 | Tag the **CI-verified commit** (plus Go companion tags in releasable mode) | Yes (state preserved, resumable) |
| 20 | Push the finalization commits and the tags | Yes (state preserved, resumable) |
| 21 | Create GitHub Release with the version's changelog section as notes | Yes (state preserved, resumable) |
| 22 | Upload assets if pipeline has `assets` or `custom_assets` configured | Yes (state preserved, resumable) |
| 23 | Run pipeline `publish` for each configured pipeline (skipped for `publish_mode: "none"`) | Yes (state preserved, resumable) |
| 24 | Deploy configured targets | No (failure recorded and named in the completion summary) |
| 25 | Run `post-release.sh` hook | No (failure recorded and named in the completion summary) |
| 26 | Regenerate the monorepo snapshot post-hoc, if the pre-push slot at step 14 was forfeit | No (failure recorded and named in the completion summary) |
| 27 | Print `Watch CI: rlsbl watch <sha>` | -- |

The tag at step 19 is placed on the commit CI verified at step 16, **not** on HEAD: the finalization commits from steps 17-18 sit on top of it and are pushed alongside it at step 20. This is what makes "the tag points at a CI-green tree" true rather than approximately true.

### The release commit

Step 18 does more than preserve the release prose. Before the archive is locked read-only, the flow writes two fields into it that record *what the version actually shipped from*:

| Field | What it records |
| --- | --- |
| `candidate_sha` | The commit CI concluded green on at step 16 — the same commit step 19 tags. Never HEAD, which by then carries the finalization commits. |
| `tree_hashes` | The git tree object of every released path as of `candidate_sha`, keyed by repo-relative path. |

**The archive is the authoritative record.** The `<!-- rlsbl-ci-sha: ... -->` marker written into the GitHub Release body at step 21 — the marker the scaffolded publish workflow's check parses to decide which commit's CI it must confirm — is a **projection** of `candidate_sha` for a consumer that cannot read the repository. It restates the release commit; it never outranks it. When the two disagree, the archive is right and the Release body is stale.

`tree_hashes` is a table rather than a single hash because what a release ships depends on the shape of the repository:

- a **standalone repository** ships everything, so the table has the single `"."` entry carrying the root tree of `candidate_sha`;
- a **workspace releasable** ships its member directories, so there is one entry per member path. No single git object covers a *set* of subtrees, so one tree hash per member is the honest record — a synthesized hash over the members would be an rlsbl invention that no git command could reproduce or check;
- a **single-member releasable** ships one directory and gets the single entry for that path.

Both fields are written by the flow and by nothing else. The editable `unreleased.toml` never carries them: neither value exists before the release runs, so one found there is either a claim about a commit that has not happened or an archive copied back without being un-finalized — and it aborts the release at validation, before any mutation. `rlsbl release undo` strips both fields when it restores an archive as the editable release file, so the freed version can be released again.

Archives written before release commits were recorded carry neither field; readers treat absence as absence and never substitute a value.

#### The stronger version that was deliberately not adopted

Recording the release commit makes the archive the authority for what a version shipped from, and the Release body's `rlsbl-ci-sha` marker a projection of it. A stronger version of the same idea was considered and rejected: making the release records the **sole** identity of a release, with tags demoted to pure projections that any repair could regenerate from the release record at will.

That is not what rlsbl does. A tag stays a real git ref with an existence of its own, and the reconciler *converges* it rather than deriving it:

- the **release record** is the authority for what was released — the version, the commit, the trees, the description;
- the **refs** are the published form of that release, and they have readers rlsbl does not control. A `git fetch` that already happened, a `go get` already resolved, a module proxy that has already cached a tag permanently — none of them will re-read a release record;
- so a ref that disagrees with the release record is a **finding**, never a thing to overwrite on the release record's word. `rlsbl release reconcile` pushes what origin is missing and re-points only what a recorded rewrite explains; a divergence no record explains aborts the whole reconcile (`refuse-foreign`), and one a transition record forbids recreating is refused outright (`refuse-identity-mismatch`). See [the five verdicts](#the-five-verdicts).

Under the rejected model every one of those refusals would be unnecessary — and a single mistaken release record entry would be sufficient authority to rewrite a namespace consumers have already resolved. Keeping both, with the release record authoritative over the *record* and the refs authoritative over *what was already published*, is precisely what lets the reconciler be fail-closed.

#### Backfilling an existing repository

`scripts/backfill_release_anchors.py` records a repository's release commits in one reviewed pass. For every archived version it resolves the version's tag under the repository's own tag spellings (`v{version}` standalone, the releasable's `tag_format` in a workspace), takes that tag's commit as `candidate_sha`, and records the tree of every released path at that commit. It also stamps the strictspec `format_version` gate onto archives written before the gate existed, and materializes an archive for a released version that never got one — recovering the description from the GitHub Release notes, then from the CHANGELOG.md section, and otherwise writing a placeholder that names the recovery obligation.

Two cases do not resolve to a tag, and neither is passed over silently:

| Case | What the pass does |
| --- | --- |
| A released version with **no tag** | Looks for the version-bump commit (whose whole message is the tag string) and records the release commit from it, noting that it did. Only when that also fails does the archive get `unrecoverable = true` — a permanent record that the commit is unrecoverable, not a temporary gap. |
| A **tag matching no released version** that still parses under a recognized scheme | Reported as operator input, with the tag name and every spelling that was probed, and the pass exits non-zero. It never guesses which version such a tag belongs to. |

Tags that parse under no recognized scheme are listed and left alone. The pass is idempotent: an archive that already carries the release commit (or the marker) and the gate is proposed for no change.

`unrecoverable` is written by the backfill and by nothing else — a flow that is releasing always knows its own candidate — and `rlsbl release undo` strips it alongside the release commit when it restores an archive as the editable release file.

### The three version fates

Every archived release file records exactly one of three fates, and every read of the release record dispatches on which:

| Fate | How it is written | What it means |
| --- | --- | --- |
| recorded | `candidate_sha` + `tree_hashes` | The version shipped, and rlsbl knows the commit and the trees it shipped from. |
| unrecoverable | `unrecoverable = true` | The version shipped, and the commit it shipped from cannot be recovered from any source. It still has consumers and real refs; only rlsbl's knowledge of where it came from is gone. |
| never released | `never_released = true` | The version NUMBER exists in the record — a phantom tag's version, a version claimed and abandoned — but no release was ever published under it. |

The third is not a degraded second. Every read that asks what this project RELEASED skips a never-released version: it is not the latest release, it does not bound the unreleased range, `rlsbl release undo` does not select it, the `unpublished-refs` check demands neither refs nor a GitHub Release for it, and `rlsbl release reconcile` never plans a deletion of a tag carrying its name. Its CHANGELOG.md section is still rendered — such a version can carry finalized changelog files, and hiding them would lose the record — annotated as never released.

An archive recording none of the three is a hard error at every read-for-use site: it was written before release commits were recorded and never backfilled, and rlsbl cannot tell which commit the version shipped from, or whether it shipped at all.

`shipped_as` is orthogonal to the three. It names the historical tag spelling a version actually shipped under when that differs from the scheme in effect today (`strictcli@v0.12.0` on a version now tagged `v0.12.0`, say). Legal on a recorded and on an unrecoverable archive; refused on a never-released one, which shipped under nothing.

### The CI gate

The gate blocks the irreversible half of the release until the repository's own CI has spoken about the candidate commit, and it distinguishes four outcomes rather than collapsing them into pass/fail. The distinction matters because the right operator response differs sharply between a definite failure, an unfinished wait, and a repository that simply has no CI to wait for:

| Verdict | What it means | What the release does |
| --- | --- | --- |
| Green | Every push-triggered run for the candidate concluded successfully | Proceeds to finalization |
| Red | At least one run definitively failed | Hard error with fix-forward guidance; nothing tagged, finalized or published |
| Timeout | The wait ran out with runs still unresolved | Hard error saying so honestly — the runs may still be in flight, so the remedy is to check them (`rlsbl watch <sha>`) and resume, not to go fix code that may be fine |
| Not configured | The repository declares no push-triggered workflow at all | Proceeds **without** a gate, and says so loudly on stderr (the notice is unconditional — `--quiet` cannot suppress it) |

Push-triggered CI that produces no runs at all within the discovery grace is a hard error, never a silent proceed. The whole wait is bounded by `--ci-timeout` (config key `ci_timeout`, default 3600s); run discovery is spent *inside* that budget and is capped at half of it, so a short budget always leaves the runs a real window to complete in.

A green workflow run is not by itself evidence that the releasing project's own CI ran, so before the release is allowed to tag, the gate reads the **jobs** of every run it watched and applies the publish gate's own name filter and conclusion policy to them. Those jobs are read through the attempt-scoped endpoint — `repos/{owner}/{repo}/actions/runs/<id>` for the run's current attempt, then `.../attempts/<n>/jobs` — and failure logs through `repos/{owner}/{repo}/actions/jobs/<job-id>/logs`. Every read is keyed by an id rlsbl already holds. The repo-level Actions collections (`.../actions/runs`, `.../actions/runs/<id>/jobs`) are never read: they 404 on some repositories where the per-run and per-attempt endpoints answer normally with the same token, and the attempt-scoped list is also the only one that names the attempt, which is what an in-place rerun (same run id, new attempt) leaves behind.

Between steps 2 and 3, four pre-mutation guards run unconditionally -- they are direct validation calls, not preflight-tag checks, so a customized pre-release hook never skips them:

- **Range pin** -- HEAD is pinned before *any* mutation (including the pre-mutating selfdoc auto-commit), and every commit the release itself creates is recorded in a trail on the state file. The pin range is re-checked at four checkpoints: the mutating entry, the candidate push, immediately after the CI gate, and the final push. A commit in the range that the release did not create -- a concurrent session sharing the worktree, an editor auto-commit, a hook -- is a hard error naming every foreign SHA with its subject. Nothing is rolled back: the guard refuses to *ship* foreign work, never to destroy it. The batch orchestrator takes the same pin at the workspace level and checks it at the batch CI gate.
- **Scaffold conflict guard** -- unresolved merge conflict markers in scaffold-managed files abort the release.
- **Cross-repo path source guard** -- a committed `pyproject.toml` (including releasable member packages) declaring a `[tool.uv.sources]` path entry that resolves outside the repository aborts the release. See the `cross-repo-path-sources` check in [checks](checks.md).
- **Version-skew guard** -- if `dev-sources.toml.local-only` declares local checkout overlays (see [dev workflow](dev-workflow.md)), each overlaid package's local version is compared against its latest PyPI release. Local ahead of the registry aborts with "release the dependency first: `<pkg>` local X > registry Y" -- the release was developed against unreleased dependency code. An unpublished overlay package or a registry/network failure is also a hard error, never a silent skip. No overlays file means nothing to check.

Steps 9 and 10 are conditionally skipped — see the hooks override mechanism below.

### Release state and resume

From the version bump onward, every step records a success or failure marker in an in-progress state file (`.rlsbl/releases/in-progress.json`; for releasable releases, `.rlsbl-monorepo/releasables/<name>/releases/in-progress.json`). If a fatal step fails (anything through pipeline publish), the state file is preserved and `rlsbl release resume` continues from where the release stopped, skipping already-completed steps including post-release steps such as asset upload.

Non-fatal failures (deploy, post-release hook, snapshot) are recorded and loudly named in the completion summary, and the release completes. The state file is cleared only when every step carries a marker and no fatal step failed; `rlsbl release run` auto-clears a provably-complete leftover state file instead of blocking.

## Who writes which ref namespace

Every namespace has one **routine writer** -- the flow that puts refs there in the ordinary course of shipping. Correcting or withdrawing something already shipped is a different job, done by a named and complete set of **repair and retraction surfaces**, listed under the table. Between the two, that is everything rlsbl writes.

| Namespace | Routine writer | Notes |
| --- | --- | --- |
| `origin` branch heads | **Releases.** `rlsbl release run` pushes the untagged candidate (step 15) and, after the CI gate, the finalization commits (step 20). | There is no dev-branch push path: `rlsbl push` does not exist, and both release entry points hard-error when the current branch is not a release branch. The pre-push hook warns on a manual push to one. The one other command that pushes a branch is `rlsbl release undo`, which pushes the revert of a version bump. |
| `origin` tags, and the GitHub Releases attached to them | **The release's tag step** (steps 19-21). | `rlsbl release reconcile` repairs them when a rewrite or a partial release left them wrong; it composes the Release through the same publication module, so the notes and the `rlsbl-ci-sha` marker are identical whichever wrote it. A released tag is never *moved*: the reconciler refuses a divergence no record explains rather than force-pushing, and the retraction surfaces delete a tag or rewrite a Release body rather than relocating one. |
| A subtree **mirror's `main`** | **The mirror reconciler's converge** -- `rlsbl monorepo mirror <project>`, and the release's mirror step, which calls the same code. | The mirror is a tool-owned derived artifact, so force-with-lease is its routine write. A commit the reconciler cannot account for is a contract violation and it refuses, touching nothing. |
| A subtree **mirror's tags**, and their GitHub Releases | **The mirror publication module**, driven by the release's mirror step or by `rlsbl monorepo mirror` materializing a released version the mirror is missing. | The commit is derived, never the branch tip: it is the subtree split of that version's recorded release commit. A mirror's scaffold renders no publish workflow, and any publish workflow reaching the mirror another way is swept on the next convergence, so a mirror never releases itself. |
| Rewritten history on any of the above | **`rlsbl release scrub`** (which wraps `safegit scrub`) -- the one sanctioned rewrite. | It force-pushes with an explicit `--force-with-lease` captured from the actual remote, then remaps the changelog hashes, re-points the tags and rewrites each tag's GitHub Release document in the same pass. A Release is edited in place, never deleted and made again, so a failure mid-pass leaves the previous document standing rather than a tag with no Release at all; only an absent Release is created. A rewrite performed outside this command leaves all three stale, and `rlsbl release reconcile` is what heals that. |

### The repair and retraction surfaces

Each of these writes one of the namespaces above deliberately, and the list is complete:

| Command | What it writes | Namespace |
| --- | --- | --- |
| `rlsbl release undo` | Deletes the GitHub Release, deletes the tag (remote and local), reverts the version-bump commit and pushes the branch. With `--version`, a non-latest release only when it is provably unpublished, and then the Release and tag only. | branch heads, tags, Releases |
| `rlsbl release reconcile` | Re-pushes the tags an out-of-band rewrite moved and writes their GitHub Release documents in place, creating only the ones origin does not have. Fail-closed: a divergence no record explains is a hard error, never a force-push. | tags, Releases |
| `rlsbl release scrub` | The rewrite itself -- see the table above. | history, tags, Releases |
| `rlsbl release edit` | Re-syncs one version's GitHub Release notes from CHANGELOG.md. | Releases |
| `rlsbl release deprecate` | Prepends a deprecation notice to a Release's body and sets its pre-release flag. | Releases |
| `rlsbl release yank` | Prepends a yank notice and sets the pre-release flag, plus the registry's own removal (npm deprecate, Go retract, a PyPI checklist). | Releases (and registries) |
| `rlsbl changelog amend` / `rlsbl changelog edit` | Rewrites a released version's JSONL, regenerates CHANGELOG.md, and re-syncs that version's GitHub Release notes. | Releases |
| `rlsbl monorepo rename-releasable` | Creates and pushes one boundary alias tag at the renamed releasable's current version, when the tag format carries `{name}`. Historical releases stay under the old prefix. | tags |

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

A release commit can touch nothing under a member's own directory. That is guaranteed on a **first** release, where the version write is a no-op, and possible on any release whose per-member writes all fall elsewhere. Without the shared entry, that member's CI job concludes `skipped` on the exact commit its tag points at -- and the gate refuses a skipped check, correctly, because a skipped check proves nothing about the commit. There is no recovery from that state either: re-running CI on the commit skips the job again, for the same reason it skipped the first time. The release commit always regenerates and commits the releasable `CHANGELOG.md` (it gains the new version's heading), so rooting every member's filter on that one path makes the gated commit verifiable for all members.

The cost is real and accepted: **releasing a releasable runs the full CI job set of every member of that releasable**, including members whose own code did not change. CI minutes are the price of never tagging a commit the gate cannot read a verdict for. The gate is not relaxed to accept `skipped` -- that would let a release publish on a commit nothing actually verified.

The same filter has a visible consequence for ordinary (non-release) pushes: a push whose diff touches only paths outside every member's filter -- a dev node project's own directory, say -- leaves every member's CI job `skipped` on that commit. For a push this is correct: nothing a member ships changed. If you need a member's CI to run on a commit that changed nothing of the member's, make the commit touch something that member's filter matches -- do not loosen the gate.

A release's **first** candidate never reaches that state, because the release commit always touches the releasable `CHANGELOG.md`. A **resumed** candidate can: when the first candidate's CI goes red and the fix-forward commits touch only the members they fix, the second candidate's push window covers only those members and every other member's job is skipped again. Widening that window would mean committing churn under paths that did not change. The exit is to re-run the same commit with the router's paths filter short-circuited:

```bash
gh workflow run ci-router.yml --ref main -f run_all=true
gh run watch <run-id>
rlsbl release resume
```

**rlsbl does this itself when it can see the state coming.** The pre-push window guard used to refuse a resume whose window is empty -- before the push, which is precisely what made its own remedy unreachable, since a dispatch resolves a ref and therefore needs the commit on the remote. When a push is owed and an earlier attempt already published a candidate, the release now pushes the candidate, dispatches `run_all` itself, correlates the created run to that commit by head SHA, and gates on it. A fresh release whose own bump commit matches none of its filters is still a hard error: that is a configuration defect, not a narrow fix-forward. See [Running every job on one commit](monorepo.md#running-every-job-on-one-commit-run_all).

Nothing is waived. Every member's real CI jobs execute on that exact commit, and a failure there still blocks the release; the gate simply reads the dispatched run's conclusions, because both gates group check runs by name across every suite on the commit and a `skipped` conclusion loses to any completed, non-skipped one of the same name -- whichever suite GitHub stamped first. See [Running every job on one commit](monorepo.md#running-every-job-on-one-commit-run_all).

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
| `rlsbl release undo` | Revert a completed release: delete GitHub Release, delete tag, revert commit, and push the reverted branch itself |
| `rlsbl release deprecate <version>` | Flag a published release as deprecated on GitHub, with an optional reason and replacement |
| `rlsbl release yank <version>` | Registry-aware removal of a published version (npm deprecate, cargo yank, Go retract, PyPI checklist) |
| `rlsbl release scrub` | Scrub sensitive content from history and re-align tags, changelog hashes and GitHub Releases |
| `rlsbl release reconcile` | Bring origin's refs and GitHub Releases back into agreement with the repository's own records (see [Reconciling published metadata](#reconciling-published-metadata)) |

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
8. Rewrites the GitHub Release document of every affected tag **in place** — the notes, the `rlsbl-ci-sha` marker taken from the (already remapped) recorded release commit, and the pre-release flag. A Release is never deleted and made again, so a failure here leaves the previous document standing rather than a tag with no Release at all; a tag carrying no Release gets one created, and a tag that parses as no version tag under any scheme is skipped without a lookup.

The command carries two selectors, each electing exactly one of its members, and the framework -- not the command -- refuses a wrong combination:

- **mode**: `--pattern <re>`, `--file <path>`, or `--recipe <toml>`. Naming none of them, or two, is a parse error.
- **commit-range**: `--from-commit <sha>` or `--entire-history`. Same rule. (File mode still requires `--from-commit`: safegit's `scrub file` has no whole-history form.)

`--replace` and `--mangle` are the match-mode replacement strategy, and they live **inside** the `--pattern` scope: they exist only while match mode is elected. Passing one under `--file` or `--recipe` is refused with the sentence naming both sides — `flag '--replace' is only valid under '--pattern', but '--file' was elected` — rather than being accepted and ignored. `--reason` is required and appears in the commit message.

Error recovery: if the command fails partway, `scrub-result.json` preserves the safegit output at `.rlsbl/releases/scrub-result.json` (for releasable releases: `.rlsbl-monorepo/releasables/<name>/releases/scrub-result.json`). Re-running the command resumes from the last completed step without re-running safegit.

The scrub also moves the **release record's release commits**. Each archived release records the commit that version shipped from (`candidate_sha`) plus the git tree of every released path. A rewrite moves those commits, and until it moved the archives too, a scrub left the tag pointing at the rewritten commit while the archive still named the old one — so every guarded release record read raised a hard error with a message accusing the tag of having moved, which was the one thing the scrub had repaired. The release commits now go through the same commit map, each released path's tree is recomputed at the rewritten commit and any change is printed, and an `release-commit-remap` transition record event records the move so a fresh clone can explain it without safegit's journal (which lives under `.git`). A rewrite performed outside rlsbl gets the same repair from two places: a scrub that finds nothing to rewrite heals the release commits from the journal on the spot, and `rlsbl release reconcile` heals them from its own merged records before it judges anything (see below).

A direct `safegit scrub` in an rlsbl-managed repository is not blocked, but it leaves rlsbl's release metadata behind: the JSONL changelogs keep pre-rewrite hashes, the release record's release commits name pruned commits, the remote's tags still point at them, and the GitHub Releases go stale. `rlsbl release scrub` does the rewrite and that repair in one pass; after an out-of-band rewrite, `rlsbl release reconcile` heals the release record's release commits and repairs what is published, and `rlsbl changelog remap --from-journal` repairs the changelog hashes.

## Reconciling published metadata

Two pieces of release metadata live outside the commit graph and outside the working tree, so nothing about a checkout makes them true: the **git refs on origin** (a version's tag, its ecosystem companion tags, the aliases a rename recorded) and the **GitHub Release** attached to each of them. A history rewrite moves the commits under them; a release interrupted after its candidate push never created them; an out-of-band deletion removes them. `rlsbl release reconcile` observes both sides, judges every subject, and — only when told to — writes the difference.

### The four explanation sources

A divergence is repaired only when something explains it, and four records can. All four are merged into one answer:

| Source | What it contributes | Survives a fresh clone |
| --- | --- | --- |
| safegit's rewrite journal (`.git/safegit/rewrite-maps.jsonl`) | the last rewrite's old-to-new commit map | no — it lives under `.git` |
| the release record (`.rlsbl/releases/v*.toml`) | each version's `candidate_sha`: where its refs belong | yes |
| the transition records (`transitions.jsonl`) | `release-commit-remap` commit maps, `boundary-alias` tags, `identity-transition` facts | yes |
| the committed scrub archives (`.rlsbl/scrubs/scrub-*.json`) | each past scrub's own old-to-new map | yes |

Successive rewrites chain: a commit rewritten twice is followed through both maps.

### The five verdicts

| Verdict | Meaning |
| --- | --- |
| `materialize` | the release record records it; origin does not have it. The ref is pushed, or the GitHub Release is created with the version's changelog section, its `rlsbl-ci-sha` marker taken from the recorded release commit, and the pre-release flag its version earns. |
| `already-correct` | both sides agree. Nothing is done. |
| `re-point-with-lease` | origin holds a different commit and a source explains it. The force-push carries an explicit `--force-with-lease` captured from the value read off origin — never a bare lease, which a rewrite has already invalidated. The Release follows the tag name by itself, so only its `rlsbl-ci-sha` marker is re-pointed. |
| `refuse-foreign` | origin holds something no source explains — **the publication tripwire**. One of these aborts the entire reconcile: nothing is repaired anywhere. A reconcile that repaired around an unexplained divergence would be choosing which half of an inconsistent world to trust. The same verdict covers a local ref that disagrees with the release record, because pushing it would publish a commit the release record does not record as released. |
| `refuse-identity-mismatch` | the target's `release_materialization_policy` refuses. Go declares it: a Go tag *is* the published artifact, so recreating one for a version released under a module path the repository has since changed would publish that version under the new identity for the first time, permanently. |

Two fates are skipped entirely. An `unrecoverable` version has no commit, so there is nothing to compare against and nothing to create a ref at. A `never_released` version was never released, so it owns no ref origin could be wrong about and no GitHub Release that could be missing — and the refs it would have owned are claimed anyway, so a tag carrying its name never reaches the unarchived-tag pass where a divergence would fire the tripwire.

### The release record is healed before anything is judged

The verdicts are computed *against* the release record, so the release record has to be true before they mean anything. An out-of-band rewrite moves the local tags and prunes the commits the archives name — and an archive naming a pruned commit makes every released ref read as disagreeing with the release record, so the tripwire aborts the whole reconcile and refuses exactly the repair the command exists to perform.

So the reconcile detects that state first — an archived `candidate_sha` that no longer resolves — and, when its own merged records explain the rewrite, moves every stale release commit through the same map before computing a single verdict. The rewritten archives and the `release-commit-remap` transition record events beside them are committed, because a rewritten read-only archive left in the working tree is breakage for every other command. Three properties:

- a dangling release commit **no record explains** is a hard error naming the version — the heal is driven by the journal, a transition record `release-commit-remap` event, or a committed scrub archive, never by resemblance;
- the content check is `refuse`: the reconcile did not perform the rewrite, so it cannot state that a released tree changing is intended (`rlsbl release scrub` is the caller that can, and it declares so);
- `--dry-run` writes nothing, release record included, and still previews the verdicts a real run would compute — the healed release commits are known without being written.

### File-driven consent

The command has one required choice, and neither half is a default:

```
rlsbl release reconcile --plan
rlsbl release reconcile --apply --approve-consequential
```

`--plan` observes origin once (one `git ls-remote`, one `gh release list`), prints the preview, and writes `.rlsbl/releases/reconcile-plan.toml` — that file *is* the preview's output artifact. It stamps a digest of the world it judged, and it is written even when it found nothing, so applying an empty plan is a clean no-op rather than an instruction to run the plan you just ran. `--dry-run` renders and writes nothing at all — under `--plan` the plan file is not written, and under `--apply` the plan is checked and the writes are only described.

`--apply` performs **exactly the repairable items the plan named**. It re-observes and refuses on two different grounds:

- the plan's `world_digest` no longer matches — origin moved, and the plan's force-push leases were captured from values that no longer hold;
- the fresh observation disagrees with the plan's own item list. `world_digest` covers the *remote*, by design (it is the lease material), so a purely **local** change between plan and apply — a tag fetched, created, or moved — leaves the digest valid while enlarging what a freshly derived preview would touch. A subject the plan does not name, a planned subject whose verdict changed, and a planned subject whose lease or target commit moved are each a hard refusal naming what was seen. Planned items that became correct on their own are reported as no-ops.

The whole command is `consequential`, so `--plan` prompts too. That is deliberate: consent is for running the command, and making it depend on which half was elected would put a flag in charge of whether a human is asked.

The GitHub Release listing is capped, and `gh release list` reports no total and offers no pagination — so a listing that comes back at the cap is a hard error naming it, never a set of unlisted Releases judged absent and proposed for creation.

Release **presence** is reported by the standing `unpublished-refs` check, which asks it of every archived version; reconcile is the repair. Its answer here is recorded to the release record: an archived version whose tag exists but whose GitHub Release does not gets a `materialize` verdict, and the Release the reconcile creates carries the same body the release flow itself would have written.

Requires **safegit 0.28.0+**. The earlier floors still apply — `--remap-shas-in`, the persisted rewrite journal, and the `cleanup_ok`/`pre_rewrite_remotes` fields (0.22.0), and destructive rewrites no longer taking `--json` as consent, so rlsbl passes `--approve-consequential` explicitly (0.25.0); safegit declares all three scrub modes `consequential`. From 0.27.0 safegit's `--json` is the strictcli framework's machine mode: stdout carries exactly one document, the envelope, and safegit's own data is its `payload` member. From 0.28.0 that envelope declares `interface_version` 2 — it grew a `writes` member, which is null on every scrub command and which rlsbl ignores — and rlsbl reads version 2 only. An older safegit's bare JSON object, and an envelope declaring version 1, are both refused by name, naming the version to install. There is no dual support and no fallback parse: upgrade safegit to 0.28.0 or later.

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

`--dry-run` previews a release by *running* the first half of it with every mutation recorded instead of performed. It is not a description written by hand alongside the code — it is the release engine itself, driven through the effects chokepoint, so what it reports is what would happen.

The preview splits at the same seam the release does:

- **Phase A — recorded.** Version bump, ecosystem keywords, lockfile syncs, the build, the release commit, and the candidate push. Every one of these is an effect, so a preview records it and the would-do log at the end of the run lists the real argv and the real byte counts. Nothing reaches the disk or the remote.
- **The boundary line.** `──────── everything below depends on CI's verdict ────────`.
- **Phase B — declared.** The CI gate, changelog finalization, the tag, the GitHub Release, asset upload, publishing, deploys and post-release hooks. These are *declared*, not recorded: their operands (which commit gets tagged, which artifacts get uploaded) do not exist until CI has judged the candidate, so the preview names each step and what it would do rather than pretending to know.

```bash
rlsbl release run --no-allow-dirty --no-watch --approve-consequential --dry-run
#   --- Recorded: Phase A (version bump -> candidate push) ---
#      1. VERSION_BUMPED       bump npm version in . -> 0.6.1
#      2. VERSION_BUMPED       build npm in .
#      3. COMMITTED            commit 2 file(s) as 'v0.6.1'  -> candidate_sha
#      4. BRANCH_PUSHED        run git push --no-verify origin <candidate_sha>:refs/heads/main
#
#   ──────── everything below depends on CI's verdict ────────
#
#   --- Declared: Phase B (CI gate -> publish), NOT recorded ---
#     CI_VERIFIED            wait for CI to go green on the candidate ...
#     TAGGED                 create v0.6.1 on the CI-verified candidate
#     ...
#
#   DRY RUN — no changes were made. Would do:
#     1. write: package.json (44 bytes)
#     ...
#     9. run: git push --no-verify origin «step 8 output»
```

`«step 8 output»` is the framework's own name for a value that does not exist: the commit the recorded commit step *would* have created. The push is rendered carrying it, which is exactly what the live push carries.

Two things a preview deliberately cannot show:

- **The secret scan.** It scans the artifacts the build produces, and the build was recorded rather than run, so there are no artifacts of this release to scan. The preview says so on the line where the scan would be.
- **Idempotency skips.** "The version is already bumped", "the remote is already at the candidate" — a preview cannot ask git anything after its first recorded mutation (the framework answers with a stale carrier, deliberately), so it assumes the release does the full piece of work. A preview therefore shows the *maximal* plan.

A preview cannot push, by construction rather than by a flag check: the push is an effect on the chokepoint, and no observe-allowlist prefix matches `git push` (`tests/test_release_phase_a_seam.py` pins both).

Called programmatically rather than through the CLI, there is no effects handle to record onto, so `--dry-run` stops at the plan summary and says so.

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

> **Check `git log` before resuming.** `rlsbl release resume` deliberately re-pins at the *current* branch tip, because the whole point is to adopt the fix commit you just made. That means it adopts **every** commit made since the failure, including another session's work sharing the worktree. Those commits ship under this version's changelog. Review the range before resuming, and move anything that does not belong onto a branch of its own.

### Recovering from a release that completed and was wrong

`rlsbl release undo` is for a release that ran to completion and then turned out to be bad — not for a CI failure, which under this ordering leaves nothing to undo. It deletes the GitHub Release, removes the git tag from both local and remote, reverts the version bump commit, and pushes the reverted branch itself — leaving the remote holding the undone state rather than the release it just removed locally. There is nothing to push afterwards.

For a version that already reached a public registry, prefer `rlsbl release deprecate` (a soft flag on the GitHub Release) or `rlsbl release yank` (registry-aware removal). An undo cannot unpublish what a registry has already served.

## Source reference

The release workflow is implemented in the `rlsbl.commands.release` module, which orchestrates the full release pipeline (see the step table above) from validation through GitHub Release creation and the post-release phase. This module coordinates version bumping, JSONL finalization, git operations, and hook execution.

:-: ref path="rlsbl.commands.release"
