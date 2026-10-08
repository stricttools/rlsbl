+++
description = "rlsbl's JSONL changelog: where its files live, the entry schema, adding and changing entries, validation, fork coverage, CHANGELOG.md, and the pre-push hook."
+++

# JSONL changelog

Each releasable keeps its changelog as JSONL files: one entry per line, each naming the commits it describes, whether users would notice, and what changed. `CHANGELOG.md` and the notes of every GitHub Release are generated from these files and the release archives; they are never edited by hand.

What this gives:

- every entry tied to the commits it describes, and every commit since the last release covered by an entry or exempt;
- entries grouped by type (breaking, features, fixes) in the generated output;
- a released version's file is read-only (mode 0444) and changes only through the commands below, each of which re-syncs that version's GitHub Release.

## Where the files live

```
.strictmetadata/changelog/<releasable>/
  unreleased.jsonl     # entries since the last release, writable
  0.27.0.jsonl         # a released version's entries, read-only
  0.26.1.jsonl
  CHANGELOG.md         # generated, in a workspace
```

At release, `unreleased.jsonl` becomes the version's file and is set read-only, and a fresh `unreleased.jsonl` is written when the next entry is added. A standalone repository writes its generated changelog to the root `CHANGELOG.md`; a workspace writes each releasable's to its changelog directory and a roll-up of every releasable to the root `CHANGELOG.md`. A file in the directory that is neither `unreleased.jsonl` nor `MAJOR.MINOR.PATCH.jsonl` is refused, naming it.

## Entry schema

```jsonl
{"format_version":2,"id":"18f3c0a6b2d4e8f0a1b2c3d4e5f60718293a4b5c6d7e8f90","commits":["a1b2c3d4e5f6..."],"user_facing":true,"type":"feature","description":"Add `--watch` to the release command."}
{"format_version":2,"id":"18f3c0a6b2d4e8f1b2c3d4e5f60718293a4b5c6d7e8f9012","commits":["e4f5a6b7c8d9...","f9a8b7c6d5e4..."],"user_facing":false}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `format_version` | always | `2`. |
| `id` | always | 48 lowercase hexadecimal digits: 16 of a nanosecond timestamp and 32 of a random UUID. Minted when the entry is written and never changed. |
| `commits` | always | The commits the entry describes, as full ids. |
| `user_facing` | always | Whether a user who upgrades would notice. |
| `type` | if user-facing | `feature`, `fix`, or `breaking`. |
| `description` | if user-facing | One line, Markdown allowed. |
| `packages` | no | In a workspace, the releasable's members the commits change; `CHANGELOG.md` prints them as a bracketed prefix. |
| `batch_reason` | no | Why an entry names more commits than the per-entry limit; it exempts the entry from `changelog-batch-commits`. |

Every line is read strictly: the generated validator of `.strictspec/changelog-entry.schema.toml` runs on each one, and a line it refuses makes the whole file unreadable, every refused line named. The `changelog-format-version` check reports the lines refused for their format version, `changelog-schema` every other refused line, and every other changelog check refuses to answer over an unreadable file. A file from before format version 2 is converted once by `rlsbl migrate records` ([the record migration](on-disk-layout.md#the-record-migration)).

Several commits may share one entry, and one commit may appear in several entries; coverage asks only that every commit appear in at least one. The batch limits are fixed, and `rlsbl changelog add --help` states both: the most commits an entry names unless it carries a `batch_reason`, and the most entries a commit appears in.

## Adding entries

```bash
rlsbl changelog add --commits a1b2c3d,e4f5a6b --description "Add --watch to the release command" --type feature
rlsbl changelog add --commits f9a8b7c --no-user-facing
rlsbl changelog add --commits <seven commits> --description "..." --type feature --batch-reason "one rename across seven modules"
```

`changelog add` acts on the releasable of the member holding the working directory. Each commit must resolve and change a file the releasable's changelog covers: its members' files and its own state directories. A commit an entry of the same type and user-facing value already names is refused. A user-facing entry needs `--description` and `--type`. `--batch-reason` is required past the per-entry limit and refused within it. The entry is committed unless `--no-auto-commit` is passed.

That commit moves `HEAD`, so `changelog add` is the last step for a piece of work: a `git commit --amend` after it amends the changelog commit, not the code. Add the entry once the code commit is settled, or pass `--no-auto-commit` and commit `unreleased.jsonl` yourself.

Every changelog command holds the release lock around its reads and writes, so two writers never lose an entry.

## Changing entries

| Command | What it does | Which file |
| --- | --- | --- |
| `rlsbl changelog add` | Appends a new entry. | `unreleased.jsonl` |
| `rlsbl changelog amend --version <v>` | Appends a new entry to a released version. `--no-validate-hashes` takes the commits as given, for an old commit a rewrite took away. The new entry's id is minted. | `<v>.jsonl` |
| `rlsbl changelog edit` | Changes one entry's description, type, or user-facing value. | any |
| `rlsbl changelog remove` | Deletes one entry. | any |
| `rlsbl changelog remap` | Rewrites stale commit ids through a map of old commits to new. | every changelog file |
| `rlsbl changelog generate` | Writes `CHANGELOG.md`. | none |

A write to a released version's file keeps it read-only, regenerates `CHANGELOG.md`, commits, and then rewrites that version's GitHub Release from the record (as `rlsbl release edit <version>` does); a rewrite that fails is an error naming `rlsbl release edit <version>`. A version without a release archive, or with one stating no fate, is refused before anything is written, and a version recorded never released has no Release to rewrite.

**`changelog edit`** is a sparse update: `--description`, `--type`, and `--user-facing` write those fields, `--unset-description` and `--unset-type` clear them, and a field not named is left as it is. At least one is required. The entry is named by `--id` or by `--commits` (an entry naming any of them), never both; several entries matching `--commits` are narrowed to those of the type the edit writes, and otherwise refused with every match named. Making an entry user-facing needs a description and a type, held already or written by the same edit.

**`changelog remove`** names its entry by `--id` or by `--commits`, never both. A selector matching several entries is refused with every match named, one matching none is refused, and a commit that no longer resolves is matched as written, since an entry naming a commit a rewrite took away is the usual one to remove.

**`changelog remap`** rewrites commit ids in every changelog file of the repository, every retired release history's included, from `--map-file` (lines of `<old> <new>`), from safegit's rewrite journal (`--from-journal`, its last rewrite), or from standard input (`--stdin`); sources may be combined, and an old commit two sources map differently is refused. Every other line keeps its bytes, an entry whose commits map to one commit names it once, and an abbreviated id the map cannot decide is refused before anything is written. What was rewritten is committed with the `Autogenerated: true` trailer. The post-rewrite git hook `rlsbl scaffold` installs runs `rlsbl changelog remap --stdin` after every amend and rebase, so entries follow their commits.

## Validation

`rlsbl check --tag changelog` runs the changelog checks for the releasable the working directory selects:

| Check | What it verifies |
| --- | --- |
| `changelog-hashes` | Every commit an entry names resolves. |
| `changelog-range` | Every resolved commit lies in the unreleased range: the commits after this checkout's nearest release commit, read from the release archives. In a workspace, a commit in the range that belongs to another releasable is reported as out of scope, naming its owner and the commands that move the entry. |
| `changelog-coverage` | Every unreleased commit that needs an entry has one. The exempt commits and the commits outside the releasable's scope are listed as notes. |
| `changelog-orphans` | No entry has every commit unresolvable, out of range, or out of scope; the finding names the `changelog remap` or `changelog remove` that fixes it, with the entry's id. |
| `changelog-schema` | Every line passes the entry schema. |
| `changelog-format-version` | Every line carries format version 2. |
| `changelog-user-facing` | At least one unreleased entry is user-facing (a warning; every release but an `infra` one refuses without one). |
| `changelog-batch-commits` | No entry names more commits than the per-entry limit unless it carries a `batch_reason`. |
| `changelog-batch-entries` | No commit appears in more entries than the per-commit limit. A declassification's squash commit, which every entry of its squashed period names, is left out of both batch checks. |
| `changelog-entry` | The generated `CHANGELOG.md` has a section for the releasable's current version. |

A commit needs no entry when it carries the `Autogenerated: true` trailer (a release commit, a regenerated file, a `rlsbl commit`), or when every file it changes is rlsbl's own bookkeeping (the changelog and release records). A commit changing no file of the releasable's members or records is outside its scope and needs no entry from it.

## Coverage in a fork

A fork declares its upstream in `.strictmetadata/upstream/upstream.toml`, a directory strictspec owns, with every field required:

```toml
format_version = 1
host = "github.com"
owner = "upstream-owner"
repo = "upstream-repo"
branch = "main"
```

A repository without that file is not a fork; no upstream is ever inferred from a git remote. In a fork, the upstream's commits are not the fork's to describe: every commit reachable from the upstream's history is left out of the unreleased range, of the pushed commits the pre-push hook covers, and of the coverage `rlsbl status` and `rlsbl monorepo status` print. The upstream's history is read from two ref namespaces:

| Ref | What it holds | Written by |
| --- | --- | --- |
| `refs/upstream/<host>/<owner>/<repo>/<branch>` | The declared branch as fetched from the upstream, here only. | The operator's `git fetch --no-tags https://<host>/<owner>/<repo> +refs/heads/<branch>:refs/upstream/<host>/<owner>/<repo>/<branch>`. rlsbl never writes it. |
| `refs/tags-of/<host>/<owner>/<repo>/<tag>` | A tag the fork inherited from the upstream, its object unchanged, here and on origin. | `rlsbl upstream adopt-tags`. |

When the declaration exists and the branch ref is missing (a fresh clone, say), the changelog checks refuse, printing the two fetches that restore the upstream's history: the branch from the upstream, and the kept tags from origin. `--no-tags` is part of both, or git would bring the inherited tags back into `refs/tags`. The branch ref holds the upstream as of its last fetch: fetch again after the upstream moves, or commits merged from its newer history are asked for entries.

The inherited tags are the upstream's releases, not the fork's, and leave `refs/tags` before rlsbl reads the fork's release record. `rlsbl upstream adopt-tags` moves each tag whose name and object the upstream also has (asked with `git ls-remote`): it writes the kept ref here, pushes it to origin, deletes the tag from origin's `refs/tags`, and deletes it here, one tag after another and one ref per push, each push guarded by the object observed, since GitHub fires no events for a push deleting four or more tags. A tag carrying an inherited name at another object refuses the whole run before anything is written. The command is consequential, `--dry-run` prints the plan, and a run after an interrupted one finishes it.

## The generated CHANGELOG.md

`rlsbl changelog generate` writes the releasable's `CHANGELOG.md` from its changelog files and release archives, newest version first, and commits what changed with the `Autogenerated: true` trailer; `--dry-run` prints the document. The release regenerates it on its own.

```markdown
<!-- Generated by rlsbl from .strictmetadata/changelog/portal/ — do not edit -->

# Changelog

## 0.6.0

The release's description, from its archive.

<details>
<summary>Context</summary>

The release's context, when it has one.

</details>

### Breaking

- ...

### Features

- [widget] Add `--format` to the graph command.

### Fixes

- ...

## 0.5.0

- No user-facing changes.
```

Only user-facing entries are listed. A version with none says so, and an `infra` release with a description lists the description under an "Infrastructure" heading. A version recorded never released keeps its section, marked as never released. A workspace's root roll-up has one `# Changelog` heading, one `## <releasable>` section per releasable, and the versions one level below.

## Changelog discipline

Changelogs are for users. The test for each entry: would a user who upgrades read it and think "that affects me"? If not, it is `--no-user-facing`.

| Change | user_facing | type |
| --- | --- | --- |
| A new command, flag, or capability | `true` | `feature` |
| A fix, described by the symptom users saw | `true` | `fix` |
| A removal, rename, or change users must act on | `true` | `breaking` |
| A performance gain users notice | `true` | `feature` |
| Tests, CI, refactoring, internal docs, lint fixes | `false` | none |

Never write a user-facing entry to get past the user-facing requirement. A release with nothing user-facing is an `infra` release, or no release at all.

## The pre-push hook

The `.git/hooks/pre-push` hook `rlsbl scaffold` installs reads git's pushed refs and, for a push to any `refs/heads/*` ref, runs `rlsbl failing-checks --hook pre-push` with the ref lines in `RLSBL_PUSH_STDIN`. A push of only tags or of `refs/backups/*` exits at once: those namespaces belong to rlsbl's release and to safegit. The selection holds:

- `prepush-changelog-coverage`: every pushed commit has an entry in the changelog of each releasable it touches;
- `prepush-gitignore-guard`: no changelog record is gitignored;
- `prepush-manual-warning`: no push to a release branch outside a release (the release pushes with `--no-verify`, so any push the hook sees there is a manual one);
- `scaffold-conflicts` and the test suite.

There is no environment-variable bypass.
