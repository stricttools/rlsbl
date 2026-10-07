+++
description = "Where rlsbl keeps a repository's records: the .strictmetadata/ directories it owns and reads, standalone versus workspace layout, run state, the old layout's residue that monorepo cleanup removes, and the one-time record migration."
+++

# On-disk layout

Every file rlsbl owns in a repository lives under `.strictmetadata/`, the directory the strict tool family shares. Each directory there is named for what it holds, carries a `manifest.toml` naming the tool that owns it, and starts with a dot when, and only when, its content is generated. rlsbl reads only these places. Two kinds of file stay outside it because readers outside rlsbl look for them there: `CHANGELOG.md` at the repository root, and the generated workflows under `.github/workflows/`.

## The directories

| Path | Authored or generated | Owner | Holds |
| --- | --- | --- | --- |
| `.strictmetadata/releasables/releasables.toml` | authored | rlsbl | The release declarations: the layout, release branches, timeouts, releasables, and members with their targets, pipelines, hooks, and checks. See [declarations](declarations.md). |
| `.strictmetadata/test-runner/test-runner.toml` | authored | rlsbl | The sandboxed test runner's settings, present only while `rlsbl:test-sandbox` is on. |
| `.strictmetadata/lifecycle-and-license/lifecycle-and-license.toml` | authored | strictspec | The [lifecycle-and-license record](lifecycle-and-license.md). |
| `.strictmetadata/options/<subject>.toml` | authored | strictspec | Options entries, rlsbl's among them ([options](checks.md#options)). |
| `.strictmetadata/upstream/upstream.toml` | authored | strictspec | A fork's declared upstream ([coverage in a fork](changelog.md#coverage-in-a-fork)). |
| `.strictmetadata/changelog/<releasable>/unreleased.jsonl` | authored | rlsbl | Changelog entries since the releasable's last release. |
| `.strictmetadata/changelog/<releasable>/<version>.jsonl` | authored, read-only (mode 0444) | rlsbl | A released version's entries. |
| `.strictmetadata/changelog/<releasable>/CHANGELOG.md` | generated | rlsbl | A workspace releasable's changelog. A standalone repository writes its changelog to the root `CHANGELOG.md` instead, and a workspace's root `CHANGELOG.md` is the roll-up of every releasable. |
| `.strictmetadata/releases/<releasable>/unreleased.toml` | authored | rlsbl | The next release's [release file](release-workflow.md#the-release-file). |
| `.strictmetadata/releases/<releasable>/v<version>.toml` | written by the release, read-only | rlsbl | A release archive with the version's fate. |
| `.strictmetadata/releases/<releasable>/version` | written by the release | rlsbl | A workspace releasable's version. |
| `.strictmetadata/releases/<releasable>/undo-audits.jsonl` | written by `release undo` | rlsbl | One line per undo, written before anything is deleted. |
| `.strictmetadata/batch-releases/unreleased.toml` | authored | rlsbl | A workspace's batch release file. |
| `.strictmetadata/transitions/transitions.jsonl` | written by the operations | rlsbl | The [transition record](conversions.md#the-transition-record): repository surgery only. |
| `.strictmetadata/history-rewrites/<UTC time>.toml` | written by the rewrite | rlsbl | One archive per `release scrub` or `transition declassify` rewrite: commit ids, tags, the mode, and the reason, never what was removed. |
| `.strictmetadata/retired-release-histories/<subject>/` | a read-only record, written by the migration | rlsbl | The changelog and release archives of a subject whose lifecycle is `retired`. Nothing releases from here, and `releasable-residue` does not report it. |
| `.strictmetadata/release-hooks/<releasable or member>/<hook>.sh` | authored | rlsbl | Hook scripts a declaration runs, where the migration moved a customized hook script. |
| `.strictmetadata/go.mod` | generated, committed | none | A stub module (`module private.invalid/rlsbl-private`, no `go` directive) that keeps `.strictmetadata/` out of the Go module zip, `go build ./...`, and `go test ./...` of a Go module at the repository root. |
| `.strictmetadata/.scaffold-state/scaffold-state.toml` | generated, committed | rlsbl | The files `rlsbl scaffold` manages with their hashes, and the rlsbl version that last scaffolded or released the repository. |
| `.strictmetadata/.scaffold-bases/<path>` | generated, committed | rlsbl | The three-way merge bases of the managed files. |
| `.strictmetadata/.cli-schema/schema.json` | generated, committed | strictcli | A strictcli program's help document, which the release writes. |
| `.strictmetadata/.release-state/` | generated, ignored by its own `.gitignore` | rlsbl | Run state: the release `lock`, and per releasable `in-progress.toml`, `retry.toml`, and `reconcile-plan.toml`; for the repository `batch-plan.toml`, `scrub-result.json`, and `declassify-result.json`. |
| `.git/rlsbl/release-checkout` | outside the tree | rlsbl | The detached [release checkout](release-workflow.md#the-release-checkout). |
| `dev-sources.toml.local-only`, `dev-overlays-state.toml.local-only` | local, ignored | rlsbl | Per-machine [dev overlays](dev-workflow.md#local-editable-overlays-rlsbl-dev-sync), which are not records. |

The run state belongs to the operator, not to the commit: it stays in the working tree while a release runs in its checkout, and its directory's `.gitignore` (`*` and `!.gitignore`) keeps it out of git. The lock is a `flock` on `.strictmetadata/.release-state/lock`, a file that is never removed: its presence says nothing, and the `lock` check reports a releasable whose `in-progress.toml` remains while no rlsbl process holds the lock.

## Standalone or workspace

`releasables.toml` states the repository's layout in `repository_layout`, and nothing else decides it:

- `standalone`: one member, the root (`path = "."`, `name = "root"`), versioned under one releasable.
- `workspace`: any number of members, versioned under one or more releasables, or under none. A workspace whose only member is the root member is still a workspace.

Every behavior that differs between the two (where `CHANGELOG.md` is written, whether the CI router is generated, which commands a directory selects) reads this field, never the member count.

## Residue of the old layout

Before these directories existed, rlsbl kept its records in `.rlsbl/` (per project), `.rlsbl-monorepo/` (per workspace), and `<member>/.rlsbl/`. rlsbl no longer reads any of them. Whatever remains of them is **residue**:

- every `.rlsbl/` directory, at the root, at a member's path, or anywhere a tracked file lies in one;
- `.rlsbl-monorepo/`;
- a versioned member's own `CHANGELOG.md` in a workspace (its releasable's changelog is generated under `.strictmetadata/changelog/`);
- the changelog validation cache of a name no releasable is declared as.

The `releasable-residue` check reports each item and says whether `rlsbl monorepo cleanup` removes it. The cleanup removes them through saferm, which keeps an audit trail and can undo a deletion, and commits the removal of what git tracked with the `Autogenerated: true` trailer. The changelog and release directories of a name no releasable is declared as are never removed: they are the record of what that subject released, the cleanup lists them as kept, and their home is `.strictmetadata/retired-release-histories/<subject>/`.

`monorepo absorb` leaves residue of the same kind: the arriving repository's `.strictmetadata/` directories other than the changelog and release directories it moves stay inside the new member until `rlsbl monorepo cleanup` removes them.

## The record migration

A repository still holding the old layout is converted once with `rlsbl migrate records`, which exists only in the source-built binary and only until every rlsbl repository has been converted. It converts the repository whose git root holds the working directory, and no other.

What it converts: every key of the old configs (each one either moved into `releasables.toml`, `test-runner.toml`, an options entry, or `strictcode.toml`, or deleted), the changelog (restamped to format version 2, moved, an id minted for every entry that lacks one, released files set back to read-only, the per-version `.md` files deleted), the release files and archives, the batch release file, the transition record (repository surgery moved, every other fact folded into the lifecycle-and-license record), scaffold state and merge bases, `.strictmetadata/go.mod` in place of `.rlsbl/go.mod`, customized hook scripts, options entries, and the settings that moved to strictcode. It writes a lifecycle-and-license record for every repository: an `active` lifecycle period and a license period per releasable, identities from the current names and tag formats and the converted renames, and registry names for every releasable that publishes and has released.

Licenses are never inferred from a `LICENSE` file: a releasable whose license its manifests do not state, and every releasable that is `proprietary`, is named in a `--licenses` file (a TOML map from releasable name to an SPDX identifier or `proprietary`), and a missing one is refused, naming the releasable.

It refuses, before anything is written: a release in progress, an uncommitted change to a path it would write or remove (uncommitted changes elsewhere are listed and left alone), a value it cannot convert, a missing license, a private GitHub repository in which no releasable is declared `proprietary`, timeouts or publish modes that disagree within the repository, a target other than go, npm, and pypi (naming the hand edit that removes it with its pipeline), and an old layout it does not recognize. `--dry-run` prints, per file, the source, the destination, and the change, and every refusal, and writes nothing. The conversion is one commit through safegit, with the `Autogenerated: true` trailer, naming the paths the migration wrote or removed and no others. A converted repository has nothing left to do, and says so.
