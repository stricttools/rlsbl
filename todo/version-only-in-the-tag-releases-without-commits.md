# The version lives only in the release tag: releases that write no files and make no commits

## Context

`rlsbl release run` does its pre-push work in a release checkout: a detached
`git worktree` at `<git common dir>/rlsbl/release-checkout`
(`rlsbl/release_checkout.py`), reused from release to release. It exists
because several agent sessions edit one working tree at the same time, while a
release writes files and commits them:

- the version files (`VERSION`, `package.json`, `pyproject.toml`, lockfiles'
  own-version entries);
- `CHANGELOG.md` and the per-version changelog files, renaming
  `.rlsbl/changes/unreleased.jsonl` to `<version>.jsonl`;
- producer output that embeds the version: the strictcli schema dump
  (`.strictcli/schema.json`) and `selfdoc gen` with `--version-override`;
- the archived release file `.rlsbl/releases/v<version>.toml` with
  `candidate_sha` and `tree_hashes`;
- `selfdoc.json`'s `version` and `versions` entries.

The checkout keeps four properties: other sessions' uncommitted work never
reaches what a release tests, generates, installs, or commits; the release
never overwrites other sessions' edits; unrelated dirty files do not block a
release; and a branch another session moved is refused (compare-and-swap in
`advance_live_branch`).

## Problem

The checkout sits inside the live repository, so every tool that searches
parent directories reaches the live tree from it:

- Go finds the live repository's gitignored `go.work`. selfdoc's local
  `go.work` (`use . ../strictspec/go`) made its release fail at the pre-checks
  hook with `main module (github.com/stricttools/selfdoc) does not contain
  package github.com/stricttools/selfdoc/.git/rlsbl/release-checkout`; where it
  does not fail, a release builds against unreleased local modules instead of
  what `go.mod` declares.
- `_find_go_work_sum` in `rlsbl/dep_locks.py` searched upward without limit and
  read the live `go.work.sum`.
- Node resolves `node_modules` through every parent directory: a probe script
  inside `.git/rlsbl/release-checkout` loaded a package from the repository
  root's `node_modules`, and did not when placed outside any repository.

A patch setting `GOWORK` for the release (off, or the checkout's committed
`go.work`) and bounding the `go.work.sum` search fixes the two Go cases only.
The checkout also costs a second copy of every repository, worktree
registration bookkeeping, `release scrub` having to remove it first, the
`live_path` mapping for operator state, and the write-back of release commits
into the live tree. Moving it outside the repository (for example under the
user cache) was considered and rejected by the owner: the goal is for the
checkout to go away entirely.

The root cause is that a release writes files. If the version is not stored in
any file, a release has nothing to write, and nothing needs isolating.

## Ruled design

The owner has ruled the following. Supported targets are reduced to go, npm,
and pypi; the other targets are being removed separately, and this design does
not have to serve them.

1. **No file holds the version.** The release tag (`v0.30.0`) is the version.
   - go: a module's version is its tag; binaries read it from the build (Go's
     VCS stamping, or goreleaser's `-X main.version={{.Version}}`). `VERSION`
     files are deleted.
   - pypi: `pyproject.toml` declares `dynamic = ["version"]` and a build plugin
     (hatch-vcs) reads the tag when building.
   - npm: the committed `package.json` (and `package-lock.json`'s root entry)
     holds a fixed placeholder version; CI writes the tag's version into its
     own checkout before `npm publish`.
   - Local builds not made from a release tag report each ecosystem's own
     development form (Go pseudo-versions, hatch-vcs development versions,
     npm's placeholder). rlsbl defines no form of its own.
2. **A release is: push, wait, tag.** `rlsbl release run` pins the current tip
   of the release branch, pushes that commit to the remote branch
   (fast-forward only), waits for CI on it, and on green creates the annotated
   tag, pushes it, and creates the GitHub Release. Publishing runs in CI from
   the tag. The release writes no file and makes no commit, so the release
   checkout, `live_path`, the write-back, the unwind, and the compare-and-swap
   branch advance are deleted; commits made after the pin are not part of the
   release and stay on the local branch.
3. **The release record is the annotated tag's message.** The description,
   the optional context, the commit CI tested, and the tree hashes go into the
   tag message as structured text, read back with `git cat-file tag`. Every
   reader of the archived release files (`status`, `unreleased`, `undo`,
   `reconcile`, `backfill`, `abandon`, scrub, the changelog generator) reads
   tags instead. A history rewrite must re-create the tags it moves with their
   messages.
4. **The release input stays a committed file.** Agents edit and commit
   `.rlsbl/releases/unreleased.toml` (bump type, description, context) before
   releasing; the release copies it into the tag message. A release refuses
   when the file still holds the description the previous release tag
   recorded, naming the file to rewrite.
5. **Changelog entries go into one append-only file**, for example
   `.rlsbl/changes/entries.jsonl`. An entry's version is the first release tag
   whose history contains the commits it covers. No file is renamed at release
   time.
6. **`CHANGELOG.md` is regenerated and committed by every
   `rlsbl changelog add`**, with pending entries under `## Unreleased`. After a
   release the committed file keeps that heading until the next
   `changelog add` regenerates it under the released version; release notes
   are rendered from the tag.
7. **Generated files never embed the version.** The strictcli schema dump
   stops stamping it, and selfdoc drops `--version-override` and any
   version-bearing generated text, so generated files change only when their
   sources change and are kept current by ordinary commits.
8. **selfdoc reads its version list from release tags**; `version` and
   `versions` leave `selfdoc.json`.
9. **Local checks read git only.** Before pushing, the release runs only checks
   that read git objects at the pinned commit: changelog coverage, the release
   file, private paths in uploads, and whether generated files match their
   sources (by recorded hash). Every build, test, producer, and hook
   (`pre-checks.sh`, `pre-release.sh`, `post-release.sh`) runs in CI on the
   candidate. A failure is fixed forward on the release branch.
10. **Existing archives are frozen.** A one-time migration leaves each
    project's `.rlsbl/releases/v*.toml` files in place as the history before
    the migration, and the migration commit records the boundary: releases
    before it are read from the archives, releases after it from tags.
    Published tags are never re-created.

## Open questions

- The tag message's text format (TOML body, trailers, or another form) and
  its schema, including how multi-line context is carried.
- How the migration is run per project (an rlsbl command with `--dry-run`),
  and what it does to `VERSION`, `pyproject.toml`, `package.json`, the JSONL
  files, and `selfdoc.json`.
- How monorepo releasables map onto tags (each releasable's tag family already
  exists) and onto one append-only entries file per releasable.
- Whether `uv.lock` records a project with a dynamic version without a version
  entry (unverified).
- How `rlsbl status`, the version-skew guard, and `dev-sources` overlays read
  versions once no file holds them.
- What the CI templates must gain: the hooks, producers in check mode, the
  npm version write before publish, and the secret and private-path scans on
  built artifacts.

## Alternatives considered and rejected

- Keep the checkout in `.git/` and set `GOWORK` for the release: fixes only
  the Go cases; kept as the interim patch until this design ships.
- Move the checkout under the user cache, with stale-copy cleanup: ends the
  parent-directory problem but keeps a copy and adds cleanup machinery.
- A temporary `git archive` export per release: keeps all four properties with
  cold caches every release; still a copy.
- A private mount namespace showing the committed tree at the live path:
  Linux-only and fragile.
- One worktree per agent session: conflicts with the single-branch,
  shared-tree workflow.
- Producers and tests in a container fed by `git archive`: needs a container
  runtime everywhere.
- Back to the live tree with scoped cleanliness checks: dirty trees block
  releases again and Node lookups still leak.
- All pre-push work in CI, returning release commits as a bundle: two CI round
  trips per release.
- A `prepare` step in the live tree plus a git-only `run`: leaks are detected
  by CI rather than prevented.
- One git-only release commit (version files written with a temporary index)
  with all building in CI: removes the checkout but keeps release commits and
  the branch write-back; superseded by keeping the version out of files.

## Affected areas

- rlsbl: `rlsbl/release_checkout.py` (deleted), `rlsbl/commands/release/`
  (the whole flow), the release-record readers, the changelog generator and
  `changelog add`, `rlsbl/dep_locks.py`, the target version writers for go,
  npm, and pypi, the scaffolded CI and publish templates, the scaffolded hooks,
  `docs/release-workflow.md`, and the version-skew guard.
- strictcli: the schema dump's version stamp.
- selfdoc: `--version-override`, version-bearing generated text, and the
  `version`/`versions` keys of `selfdoc.json`.
- Every rlsbl-managed project: the one-time migration.

## Effort

Several weeks: the release flow rewrite and record readers in rlsbl, the CI
templates, the selfdoc and strictcli changes, and the migration of every
managed project.
