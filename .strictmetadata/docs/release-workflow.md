+++
description = "The rlsbl release flow: the release file, validation and its refusals, the release checkout, the pre-release pipeline and preflight, the step table from the version bump to the post-release hooks, the untagged candidate and its CI verdict, resume and abandon, the three version fates, the publish workflow's wait-for-ci job, and the commands that act on past releases."
+++

# Release workflow

`rlsbl release run --watch` releases one releasable: it validates everything it can before writing anything, writes the new version, commits it, **pushes that commit to the release branch untagged and waits for the repository's own CI to conclude on it**, and only then finalizes the changelog, archives the release, tags the commit CI verified, pushes, creates the GitHub Release, and publishes.

This ordering is the property the whole flow rests on: the tag, the GitHub Release, the finalized changelog, and every registry write happen after a green CI verdict on the commit being released. A red verdict leaves nothing behind but the candidate commit on the branch: no tag, no Release, no finalized changelog, nothing on any registry. The version is never burnt by a failure; the fix is committed on the release branch and `rlsbl release resume --watch` completes the same version.

## Quick reference

```bash
# Write the release file, then edit its bump and description
rlsbl release init

# Preview, then release
rlsbl release run --no-watch --dry-run
rlsbl release run --watch --approve-consequential

# After a red CI verdict: fix, record the fix, resume the same version
rlsbl changelog add --commits <fix> --description "..." --type fix
rlsbl release resume --watch --approve-consequential
```

`--watch` or `--no-watch` is required, with no default: `--watch` watches the CI runs of the release commit (the publish runs the Release started among them) to completion in-process after the release, then asks the npm and PyPI package listings whether they list the version, asking again over a short wait while they catch up; `--no-watch` says the outcome was not verified and prints the `rlsbl watch <sha>` that watches the runs. `--push-timeout`, `--ci-timeout`, `--check-timeout`, and `--hook-timeout` override the declared [timeouts](declarations.md#top-level-keys) for one run. In a workspace, `--releasable` names the releasable where the working directory selects none (the workspace root), and is refused where it selects one. `release run` and `release resume` are consequential: `--approve-consequential` skips the confirmation, which a non-interactive run cannot answer.

## The release file

The release file, `.strictmetadata/releases/<releasable>/unreleased.toml`, states what the next release is. `rlsbl release init` writes it, with `bump` and `description` blank for a person to fill in, `context` blank, every target of the releasable's members in `include`, and `exclude` empty, and commits it. A release file nobody filled in yet is left as it is, one somebody filled in is refused rather than overwritten, and a member versioned under no releasable is refused, naming `rlsbl monorepo release init`. A filled-in file reads:

```toml
format_version = 2
bump = "minor"                  # patch, minor, major, or infra
description = "Add retry logic and fix timeout handling"
context = """
Optional prose on why these changes were made.
"""
include = ["npm", "pypi"]       # the targets to release
exclude = []                    # targets to skip; disjoint from include
```

| Bump | Moves the version | Use for |
| --- | --- | --- |
| `patch` | 0.5.2 to 0.5.3 | fixes and small changes |
| `minor` | 0.5.2 to 0.6.0 | new capabilities, and breaking changes before 1.0.0 |
| `major` | 0.5.2 to 1.0.0 | breaking changes after 1.0.0 |
| `infra` | as `patch`, recorded as infra | a release with no user-facing entry |

Every release but `infra` refuses to run without a user-facing changelog entry, naming `bump = "infra"`; an `infra` release refuses one. `description` is required and may not be blank; `context` is optional and renders as a collapsible block in `CHANGELOG.md`. Both remain after the release in the version's archive, which every later regeneration reads. Versions are `MAJOR.MINOR.PATCH`: there is no pre-release channel, and a file carrying `preid`, `blog`, or `bump = "prerelease"` is refused.

The fields the release itself records (`release_commit`, `released_trees`, the fates, `shipped_as`, and `release_notices`) are refused in the editable file: no value of them exists before the release runs.

## Validation

Before it writes anything, the release refuses, each refusal naming its fix:

- a working directory that selects no releasable, or a member versioned under none;
- an unfinished release: an `in-progress.toml` names `rlsbl release resume --watch` or `rlsbl release abandon`, by its version's fate;
- a releasable, or a member versioned under it, whose lifecycle is `on-hold` or `retired`;
- a missing or unreadable release file, a target it includes that no member of the releasable has, and a bump the user-facing rule refuses;
- an environment file that cannot be read;
- a stash (work git names in no working tree, which the release would neither see nor carry);
- a local pipeline whose credentials are missing, and a pipeline the lifecycle-and-license record or the repository's visibility forbids ([private repositories](pipelines.md#private-repositories-and-proprietary-releasables));
- a GitHub visibility that disagrees with the record (a proprietary releasable in a public repository, a private repository with no proprietary releasable), and a `deploy_command` on a releasable that is not a proprietary server;
- a record that drops a closed period or a held registry name present at the releasable's nearest release commit;
- a dev overlay ahead of PyPI: a `dev-sources.toml.local-only` overlay whose local version is above the package's latest PyPI release means the release was developed against unreleased code, and the dependency is released first;
- a Go module whose `go mod tidy` would change it, and a Go workspace whose `go work sync` would raise a requirement;
- a next version that cannot be decided ([the version decision](#the-version-decision)), and a tag of it here or on origin;
- `gh` unauthenticated, no push access to the repository, a fetch of origin that fails, a branch that is not a declared release branch, a branch behind origin, and a releasable publishing from CI whose GitHub Release would start no publish workflow.

## The release checkout

A release never runs in the working tree. `rlsbl release run --watch`, `rlsbl release resume --watch`, and `rlsbl monorepo release run --watch` check out the commit the release branch points at into the **release checkout**, a detached `git worktree` at `<git common dir>/rlsbl/release-checkout`, and run there: the producers, the hooks, the tests and checks, the version bump, the build, and every commit the release makes. The checkout is reused from release to release: each release resets it to the commit it starts from and removes its untracked files, keeping ignored ones so dependency environments and build caches stay warm. `rlsbl release scrub` removes it before rewriting history.

Its commits reach the release branch only by a **compare-and-swap advance** (`git update-ref <branch> <new> <old>`): the branch moves only from the commit this release last left it at, and a branch another session moved meanwhile is refused, naming the commits that appeared. Only the files the release's own commits change are written into the working tree; each must be free of uncommitted changes, or the advance refuses, naming it.

Before anything runs, the release reads the working tree once. An uncommitted change inside the paths the release writes (the releasable's changelog, release, and run-state directories, its generated changelog and a workspace's roll-up, the scaffold state, each member's `selfdoc.json`, and the version files and lockfiles of its members' targets) refuses the release, naming each path. Every other uncommitted change is listed and left alone. `--dry-run` reports both lists instead of refusing, takes no lock, creates no checkout, and previews the release against the working tree as it stands.

Every process the release starts in the checkout gets this environment:

| Variable | Value |
| --- | --- |
| `RLSBL_RELEASE_BIN` | `<git common dir>/rlsbl/release-bin`, the release's own directory for binaries, empty when each release starts. |
| `PATH` | `$RLSBL_RELEASE_BIN` first, then the `PATH` rlsbl was started with. |
| `GOWORK` | The checkout's own `go.work` when the repository tracks one at its root, `off` otherwise, so Go never builds against the working tree's uncommitted `go.work`. |

A project whose release must run its own unreleased build of a tool (selfdoc releasing itself runs the selfdoc it is about to ship) builds it into `$RLSBL_RELEASE_BIN` from a pre-checks hook; the copy installed for every other session is never touched.

The run state (`.strictmetadata/.release-state/`), the lock, a relative environment file, and the dev overlay files stay in the working tree: they belong to the operator, not to the commit. Because the checkout holds only committed files, the release's tests run against what the lockfiles name, never against local overlays.

## The pre-release pipeline and preflight

In the checkout, in this order:

1. the `pre_checks` [hooks](declarations.md#hooks) of the releasable and its members;
2. the strictcli schema dump, for a project strictcli detection finds: the program's `help --json`, written to `.strictmetadata/.cli-schema/schema.json` with its version set to the version being released;
3. `selfdoc gen --no-auto-commit --version-override <version>`, for a project using selfdoc;
4. `selfdoc check` with the same override;
5. the selfdoc commit: the files the selfdoc steps wrote, committed with the `Autogenerated: true` trailer and recorded among the release's own commits;
6. the changelog validation (the `preflight-changelog` selection) and the preflight (the `preflight` selection, which is the `pre-release` hook of `checks.toml`), both blocking on error-level failures only;
7. the built-in tests of each member, unless the member or the releasable declares a `pre_release` hook;
8. the `pre_release` hooks of the members and then of the releasable.

The preflight always runs every check it selects, built-in and external alike, whether or not hooks are declared: a declared `pre_release` hook replaces only the member's built-in tests. Under `--dry-run` the pure checks run and the impure ones are listed.

## The release steps

Once validation and the pre-release pipeline pass, every step records its outcome in `.strictmetadata/.release-state/<releasable>/in-progress.toml`. A fatal step's failure stops the release with its state kept, so `rlsbl release resume --watch` continues from it; the one non-fatal step's failure is recorded and named in the closing summary.

| Step | What it does | Fatal |
| --- | --- | --- |
| `version-bumped` | Writes the releasable's version file, its members' target version files, and each member's `selfdoc.json` version; adds the `rlsbl` keyword while `rlsbl:ecosystem-tagging` is on; re-locks the lockfiles the bump made stale; records the running rlsbl in the scaffold state; cleans and builds the artifacts; scans them for secrets with gitleaks; checks that every packed file comes from the releasable's members; and refuses any change the release did not make. | yes |
| `committed` | Commits the version bump. | yes |
| `candidate-pushed` | Advances the branch to the commit and pushes it **untagged**: the release candidate. | yes |
| `ci-verified` | Waits for the repository's own CI on the candidate ([the CI verdict](#the-ci-verdict)). | yes |
| `changelog-finalized` | Turns `unreleased.jsonl` into the version's read-only file and regenerates `CHANGELOG.md`. | yes |
| `release-archived` | Archives the release file as `v<version>.toml`, recording the release commit and the released trees, and turns any pending identity of this version in the [lifecycle-and-license record](lifecycle-and-license.md) into a dated period. | yes |
| `tagged` | Tags the CI-verified commit, with the Go [companion tags](targets.md#companion-tags). | yes |
| `pushed` | Pushes the finalization commits, then the tags one per push, the primary tag first: GitHub creates no events for a push of more than three tags. | yes |
| `github-release-created` | Creates the GitHub Release on the pushed tag, with the version's changelog section as its notes and the `rlsbl-ci-sha` marker naming the release commit. | yes |
| `pipelines-published` | Publishes the pipelines that publish from this machine ([pipelines](pipelines.md#publishing-from-this-machine)); CI pipelines publish from the publish workflow the Release starts. | yes |
| `deployed` | Runs the releasable's `deploy_command`, `{version}` replaced, from the repository root, with the check timeout. A server releasable's deploy that fails leaves the release resumable, and `rlsbl release resume --watch` runs it again for the same version. | yes |
| `post-release-hooks-run` | Runs every `post_release` hook. | no |

The tag points at the commit CI verified, not at `HEAD`: the finalization commits sit on top of it and are pushed with it. After the steps, the release confirms that every workflow a published Release starts shows a run for the tag, then watches the runs and asks the registries' package listings for the version (`--watch`), or prints the command that watches them (`--no-watch`).

A failure before the candidate push leaves nothing to undo: the attempt existed only in the checkout, the branch and working tree are as they were, and a fresh release's state file is deleted. When the candidate push is refused after the advance, the advance is taken back by the same compare-and-swap, restoring only the files it wrote. There is no `git reset --hard` anywhere in a release.

### The range pin

`HEAD` is pinned before any change, and every commit the release creates is recorded in its state. The range is checked again at the mutating entry, before the candidate push, after the CI verdict, and before the final push: in a fresh run, a commit the release did not create (another session sharing the worktree, an editor's commit, a hook) refuses the release, naming each foreign commit with its subject. Nothing is rolled back: the release refuses to ship foreign work, never destroys it.

## The CI verdict

The release waits in-process for every push-triggered workflow run on the candidate and reads one of four verdicts:

| Verdict | What it means | What the release does |
| --- | --- | --- |
| green | Every run concluded successfully. | Goes on to finalize. |
| red | A run concluded in failure. | Stops, with the fix-forward steps; nothing is tagged, finalized, or published. |
| timeout | Runs did not conclude within `ci_seconds`, or their state could not be read. | Stops, saying so: the runs may still be going, so check them with `rlsbl watch <sha>` and resume. |
| no CI | The repository declares no push-triggered workflow. | Goes on without a CI check, saying so on stderr whatever `--quiet` says. |

A red verdict comes from the run's own state, never from `gh`'s exit code alone: a watch that drops out is resumed within the same budget, and a failure is run again once only when its failing jobs' logs show a failure outside the code (an infrastructure failure re-runs only its failed jobs). Runs discovered after the first listing are watched too, and push-triggered CI that produces no run within the discovery window is an error, never a pass.

A green workflow run does not by itself show that the releasing project's own CI ran, so before it tags, the release reads the jobs of every run it watched and requires the project's own CI jobs among them, passed. In a workspace a member's jobs are skipped on a candidate that changes none of the files its router filter covers. A fresh release whose candidate would leave a member of the releasable skipped is refused before the push, since that is a defect of the filters. A resumed release whose fix-forward commits touch only some members, after an earlier attempt already pushed a candidate, instead pushes the new candidate, dispatches the CI router with `run_all=true` on it, and reads that run ([running every job on one commit](monorepo.md#running-every-job-on-one-commit)).

## Resume, abandon, and the version decision

### Resuming adopts the branch

A stopped release leaves its branch open, and work continues there: the fix committed after a red verdict, and whatever else another session commits beside it. `rlsbl release resume --watch` takes the branch as it stands: it pins again at the current tip, so every commit since the original pin is adopted, pushed as the new candidate, judged by CI, and contained in the tagged commit. The version stays the one the state file records, and the steps already done keep their outcomes, except that adopting past the CI verdict drops it: the tip is a new candidate.

Adoption has one condition, checked before anything changes: every adopted commit the release did not create has a changelog entry, or needs none under the rules [changelog coverage](changelog.md#validation) applies. An uncovered commit is refused by id and subject, with the `rlsbl changelog add` that records it. Read `git log` before resuming: another session's commit on the branch ships under this version.

While a state file exists, `rlsbl release run --watch` refuses and names `rlsbl release resume --watch`.

### Abandoning an attempt

`rlsbl release abandon` records a release that will not be finished: it writes the version's archive with `never_released = true`, commits it with the `Autogenerated: true` trailer, and deletes the state file, as one operation. The attempt's commits are not reverted; the version files naming the abandoned version are where the next release starts. An abandon that stopped part-way is finished by running it again. It refuses, before writing anything, when there is nothing to abandon, when the version is already archived as a release, when it is below the latest release, when the attempt's tag exists here or on origin (a tag is evidence of a release, which `rlsbl release backfill` adopts), and when a GitHub Release exists under the attempt's tag (a published release, which `rlsbl release undo` reverts).

### The version decision

The next version comes from the version files and the release record, never from an archive's mere existence:

- A **first release**, whose record holds no released version, ships the version files' version as it is; the declared bump is ignored. In a workspace each releasable asks its own record.
- Version files naming a **released** version, or one recorded **never released**, are bumped from (a `minor` bump from a never-released 0.29.4 gives 0.30.0).
- Version files naming a version recorded **unrecoverable** whose tag is gone are refused, naming the tag to restore at the commit it shipped from.
- Version files naming a version with neither an archive nor a tag are refused: above the latest release, that is what an abandoned attempt leaves, and the refusal names `rlsbl release abandon`; below it, the version files are behind and must name at least the latest release.
- A next version the record holds as **never released** is refused: a never-released number is never used again, and the refusal names the version-files value that bumps past it.

## The three version fates

Every archive records one of three fates, and every question about what the releasable released reads them:

| Fate | Written as | Meaning |
| --- | --- | --- |
| recorded | `release_commit` and `[released_trees]` | The version shipped, from that commit; the trees are the git tree of every released path at it (`"."` for a standalone repository, one entry per member directory in a workspace). |
| unrecoverable | `unrecoverable = true` | The version shipped, and the commit it shipped from cannot be recovered. Its refs and consumers exist; only the record of its origin is lost. Written by the backfill, and by `transition declassify` for a release whose commit was folded into a squash. |
| never released | `never_released = true` | The version number exists in the record, and nothing was ever published under it. |

A never-released version is not a release: it is not the latest release, does not bound the unreleased range, is not undone, and owns no refs or Release. Its changelog section is still rendered, marked as never released. An archive stating no fate is an error wherever the record is read for use, naming the backfill.

`shipped_as` names the tag a version shipped under when it differs from the tag its releasable's format gives it now (after a rename, say). That tag stays the version's primary ref: the one its Release hangs off and the one `release edit`, `deprecate`, `yank`, `retry`, and `reconcile` resolve. It is allowed on a recorded or unrecoverable archive, and refused on a never-released one.

**The archive is the record of what was released; the refs are its published form.** The `rlsbl-ci-sha` marker in a Release body restates the release commit for CI, which cannot read the repository, and never outranks it. A ref that disagrees with the record is a finding to repair through `rlsbl release reconcile`, which re-points only what a recorded rewrite explains; it is never overwritten on the record's word, because a fetch, a `go get`, or the module proxy may already have resolved it.

## The publish workflow

A GitHub Release starts the publish workflow, which would race CI on the same commit if nothing held it. Every publish workflow rlsbl generates therefore starts with a `wait-for-ci` job, and every publish job needs it. The job resolves the release commit from the `rlsbl-ci-sha` marker of the tag's Release, else from the tag's commit, and polls that commit's check runs until the releasing project's CI concluded, matching check-run names against `CI_CHECK_PATTERN`:

| CI conclusion | The job |
| --- | --- |
| `success` on every matching check run | passes; the publish jobs run |
| `failure` or `timed_out` | fails: the code at this commit is broken, and the fix is a new release, never a retried publish |
| `cancelled` | fails: a cancelled run proves nothing about the commit; re-run that CI, then dispatch the publish workflow at the tag |
| `skipped` | fails: the project's own CI must run on the release commit |
| no matching check run within `WAIT_GRACE_MINUTES` | fails |
| still running past `WAIT_TIMEOUT_MINUTES` | fails, listing each pending check run |

The limits are the job's env (`WAIT_TIMEOUT_MINUTES`, `WAIT_GRACE_MINUTES`, `WAIT_POLL_SECONDS`), and the job's own `timeout-minutes` must be raised with the timeout. Same-named check runs count as the newest one, except that a skip never outranks a verdict of the same job. Under rlsbl's ordering CI already went green on the commit before it was tagged, so the job confirms rather than waits.

A publish run that failed for reasons outside the code (a registry outage, an expired token) is run again at the tag, never at a branch: `rlsbl release retry --watch`, or `gh workflow run publish.yml --ref <tag>`. Publish workflows queue one run per tag and never cancel one in flight.

## Who writes which ref namespace

Each namespace has one routine writer, and a closed set of repair and retraction commands corrects or withdraws what already shipped. A write from anywhere else is not rlsbl's.

| Namespace | Routine writer |
| --- | --- |
| `origin` branch heads | Releases: the candidate push, and after the CI verdict the finalization commits. There is no other push path, and the pre-push hook refuses a manual push to a release branch. |
| `origin` tags and their GitHub Releases | The release's tag and Release steps. A shipped tag is never moved by the routine writer. |
| Rewritten history | `rlsbl release scrub` (and `rlsbl transition declassify`), through safegit: the force-push, the re-pointed tags, and each Release rewritten in place in one pass. |
| A fork's inherited tags, `refs/tags-of/<host>/<owner>/<repo>/<tag>` | `rlsbl upstream adopt-tags`. |
| A fork's upstream branch, `refs/upstream/<host>/<owner>/<repo>/<branch>`, here only | The operator's `git fetch --no-tags`; rlsbl only reads it. |

The repair and retraction commands:

| Command | What it writes |
| --- | --- |
| `rlsbl release undo` | Deletes a release's GitHub Release and tags, reverts its version-bump commit (the latest release only), restores its changelog and release file, and pushes the branch. |
| `rlsbl release reconcile` | Pushes the refs origin lacks, force-pushes with a lease the ones a recorded rewrite moved, and creates the absent Releases. |
| `rlsbl release edit` | Rewrites one Release from the record. |
| `rlsbl release deprecate`, `rlsbl release yank` | Record a notice in the archive and rewrite the Release with it on top, marked pre-release; `yank` also makes each registry's own removal. |
| `rlsbl changelog amend`, `edit`, `remove` | Rewrite a released version's changelog and then its Release. |
| `rlsbl monorepo rename-releasable` | Pushes one boundary alias tag at the renamed releasable's current version. |
| `rlsbl upstream adopt-tags` | Moves a fork's inherited tags out of `refs/tags`, here and on origin. |

## Acting on past releases

| Command | What it does |
| --- | --- |
| `rlsbl release edit [version]` | Rewrites a version's GitHub Release in place from the record: the notes its changelog holds, the notices its archive records on top, the `rlsbl-ci-sha` marker, and the pre-release flag (set when a notice is recorded). Defaults to the latest release. |
| `rlsbl release retry --watch` | Dispatches the latest release's workflows again at its tag, from `.strictmetadata/.release-state/<releasable>/retry.toml` (written, when missing, naming every workflow of the tagged tree with a `workflow_dispatch` trigger). A ref other than the tag is refused. A dispatch that fails leaves the file holding the workflows not yet dispatched, so no workflow is dispatched twice. |
| `rlsbl release undo [--version <v>]` | Reverts a release that is provably unpublished: the registries' package listings, origin's Go module tags, and the publish runs of its tag must show it absent and none may show it published. Without `--version` the latest release, its version-bump commit reverted; with it an earlier one, its commits left. An audit line is committed to `undo-audits.jsonl` before the first deletion. |
| `rlsbl release deprecate <version> --reason <text> [--use <version>]` | Records `> **Deprecated:** <reason>. Use v<use> instead.` in the archive's `release_notices`, commits it, and rewrites the Release with the notice on top, marked pre-release. |
| `rlsbl release yank <version> --reason <text> [--use <version>]` | For each published package: npm deprecates the version, a Go module gains a `retract` line (committed, published by the next release), and PyPI is yanked by hand, the command exiting 1 naming the steps until PyPI's project document shows the files yanked. Records the yank notice and rewrites the Release. Nothing is ever unpublished, and a proprietary releasable is refused. |
| `rlsbl release scrub` | Rewrites history through safegit and repairs every record the rewrite renamed ([scrubbing](#scrubbing-history)). |
| `rlsbl release reconcile --mode plan\|apply` | Repairs origin's refs and Releases from the record ([reconciling](#reconciling-published-metadata)). |
| `rlsbl release backfill` | Brings old archives into the fate model ([backfilling](#backfilling-an-existing-repository)). |

`deprecate` and `yank` refuse the latest release (which `undo` reverts), a version the record holds no release of, and one without a Release. Every command here acts on the releasable of the member holding the working directory and takes the release lock.

### Scrubbing history

`rlsbl release scrub` removes content from git history through safegit and repairs what the rewrite renamed, in one pass:

```bash
rlsbl release scrub --pattern 'secret_token_[a-z0-9]+' --replace REDACTED --entire-history --reason "remove a leaked token"
rlsbl release scrub --file config/secrets.yml --from-commit a1b2c3d --reason "remove a secrets file"
```

The mode is one of `--pattern` (each match replaced by `--replace`, or by random text of the same length under `--mangle`), `--file` (every past version replaced by the copy on disk, or removed where there is none), or `--recipe` (a safegit recipe); the range is `--from-commit` or `--entire-history`. The scrub removes the release checkout, runs safegit (which remaps the changelog's commit ids at every rewritten commit), requires every changelog commit id to resolve (repairing from safegit's rewrite journal where it can), moves each archive's release commit through the rewrite with the rewritten trees and a `release-commit-remap` event in the transition record, requires every generated changelog to match a fresh generation, writes a history-rewrite archive (commit ids, tags, mode, and reason, never what was removed), and commits. It then force-pushes the branch and each moved tag with a lease on what origin held before, and rewrites each moved tag's Release in place; a Release is never deleted. A scrub that stops is finished by running the same command again. It requires the safegit version `rlsbl release scrub --help` names, or a newer one, and a release branch, and is refused while a release is stopped mid-flight.

A rewrite made outside rlsbl leaves the changelog's commit ids, the archives' release commits, origin's tags, and the Releases behind: `rlsbl changelog remap --from-journal` repairs the changelog, and `rlsbl release reconcile` the rest.

### Reconciling published metadata

`rlsbl release reconcile` judges every archived version's refs and Release, and every local tag origin holds that no archive claims, against the record:

| Verdict | Meaning |
| --- | --- |
| `materialize` | The record holds it and origin does not: the ref is pushed, or the Release created with the body the release writes. |
| `already-correct` | Both agree. |
| `re-point-with-lease` | Origin holds another commit, and safegit's journal, a `release-commit-remap` event, or a history-rewrite archive explains the move: force-pushed with a lease on the value read from origin. |
| `refuse-foreign` | Origin holds something nothing explains. One such verdict aborts the whole reconcile, and nothing anywhere is written. |
| `refuse-identity-mismatch` | A Go tag of a version released under a module path or repository the lifecycle-and-license record has since closed: recreating it would publish that version under the new identity for good. |

Unrecoverable and never-released versions are passed over, and so are the tags the record keeps outside the version model or a closed identity owned. Archives naming a commit the repository no longer has are first moved through the records that explain the move, and committed; one nothing explains is refused.

Consent is file-driven: `--mode plan` writes `.strictmetadata/.release-state/<releasable>/reconcile-plan.toml` (an empty plan included), and `--mode apply` observes again and refuses when origin or the Release listing moved since the plan, or when the observation finds work the plan does not name, before performing the plan.

### Backfilling an existing repository

`rlsbl release backfill` brings every releasable's archives into the fate model from the repository's history: it records each version's release commit from its tag (or the `shipped_as` spelling, or its version-bump commit), completes an archive whose required fields are missing or blank, writes an archive for a released version that has none, and adopts a version tag no record names as the release it is evidence of. A version with no tag and no version-bump commit is recorded unrecoverable. A never-released version is declared, not inferred: write its archive with `never_released = true` first, and the backfill leaves it alone.

A reconstructed description comes from the first source that yields one: `--overrides` (`[versions."X.Y.Z"]` tables with `description` and an optional `context`), the version's GitHub Release body, its `CHANGELOG.md` section, the commit subjects of its tag range, and otherwise a placeholder naming the obligation; each written field names its source. Every tag nothing accounts for is listed first and refuses the whole apply, with the three ways out: adopt it, record it with `rlsbl transition unversioned-tag`, or delete it. `--dry-run` prints the plan and exits 1 when an unexplained tag would refuse the apply; the apply is consequential.

## Examples

```bash
rlsbl status
rlsbl unreleased                       # which commits still need an entry
rlsbl changelog add --commits a1b2c3d --description "Add retry logic to the HTTP client" --type feature
rlsbl changelog add --commits e4f5a6b --no-user-facing
rlsbl check --tag changelog
rlsbl release init                     # then set bump = "minor" and the description
rlsbl release run --watch --approve-consequential
```

When CI goes red on the candidate there is nothing to undo: fix the failure on the release branch, record the fix, and run `rlsbl release resume --watch`. Do not start a new release at a higher version to get past a red CI, and do not run CI again on the same commit expecting another answer. A timeout verdict differs only in that the runs may still be going: check them with `rlsbl watch <sha>` before deciding anything needs fixing.

`rlsbl release undo` is for a release that completed and turned out wrong, never for a CI failure. For a version a registry already served, prefer `rlsbl release deprecate` or `rlsbl release yank`: an undo cannot take back what a registry handed out.
