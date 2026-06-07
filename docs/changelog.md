---
description: "JSONL changelog system reference — entry schema, adding entries, 10 validation checks, generation, amending released versions, and pre-push enforcement."
---

# JSONL changelog

## Overview

rlsbl uses structured JSONL files as the source of truth for changelogs. Each commit (or group of commits) gets a JSON entry describing what changed and whether it affects users. `CHANGELOG.md` is fully generated from these JSONL files — never edit it by hand.

This system provides:
- Per-commit audit trail linking changelog entries to exact git commits
- Automated coverage checks ensuring no commit ships without documentation
- Type-based grouping (breaking/features/fixes) in the generated output
- Immutable per-version files after release (chmod 444)

## Directory layout

```
.rlsbl/changes/
  unreleased.jsonl     # Current work — writable, one JSON object per line
  0.27.0.jsonl         # Released version — read-only (chmod 444), immutable
  0.27.0.md            # Generated markdown for this version
  0.26.1.jsonl         # Older released version
  0.26.1.md            # Generated markdown for this version
  .validated           # Cache file — HEAD hash of last successful validation
```

- `unreleased.jsonl` accumulates entries between releases
- At release time, it is renamed to the version file (e.g., `0.28.0.jsonl`) and locked read-only
- Per-version `.md` files are generated alongside JSONL files for quick reference
- `CHANGELOG.md` at the project root aggregates all versions (newest first)

## Entry schema

Each line in a JSONL file is a self-contained JSON object:

```json
{"commits":["a1b2c3d"],"user_facing":true,"description":"**New feature.** What it does for users.","type":"feature"}
{"commits":["e4f5g6h","i7j8k9l"],"user_facing":false}
```

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `commits` | Always | array of strings | Commit hashes (any prefix length — resolved to full SHA at validation) |
| `user_facing` | Always | boolean | Whether this change affects users |
| `description` | If user_facing | string | One-line description, supports markdown (bold, backticks) |
| `type` | If user_facing | string | One of: `feature`, `fix`, `breaking` |

Non-user-facing entries need only `commits` and `user_facing: false`. They exist solely for commit coverage and are excluded from generated output.

### Multiplicity rules

- Multiple commits can map to one entry (one feature spanning several commits)
- One commit can appear in multiple entries (a commit that fixes a bug and adds a feature)
- The coverage check only requires that every commit appears in at least one entry

## Adding entries

Use `rlsbl changelog add` after each commit or group of related commits:

```bash
# User-facing entry
rlsbl changelog add --commits a1b2c3d,e4f5g6h --description "Add --watch flag to release command" --type feature

# Non-user-facing entry
rlsbl changelog add --commits f9g8h7i --no-user-facing
```

| Flag | Description |
| --- | --- |
| `--commits` | Required. Comma-separated commit hashes. |
| `--description` | Required for user-facing entries. One-line description. |
| `--type` | Required for user-facing entries. One of: `feature`, `fix`, `breaking`. |
| `--no-user-facing` | Mark as non-user-facing (no description or type needed). |
| `--no-commit` | Skip the auto-commit of `unreleased.jsonl`. |

The command:
1. Resolves each hash via `git rev-parse` (errors on invalid hashes)
2. In monorepo mode, validates that each commit touches files within the current sub-project's path/watch patterns
3. Validates entry schema
4. Appends to `unreleased.jsonl`
5. Auto-commits the file (message: `changelog: <description>`) unless `--no-commit` is passed

## Validation

`rlsbl check --tag changelog` runs 10 checks. All must pass before a release proceeds.

| # | Check | What it verifies |
| --- | --- | --- |
| 1 | Hash resolution | Every hash in every entry resolves via `git rev-parse` |
| 2 | Tag-based range | Every resolved hash is in the unreleased range (`<last_tag>..HEAD`). Uses `git describe --tags --abbrev=0 --match 'v*'` to find the last tag. |
| 3 | Commit coverage | Every unreleased commit appears in at least one entry. Commits that only touch `.rlsbl/changes/` or `CHANGELOG.md` are auto-exempted. |
| 4 | Orphan detection | Entries where ALL hashes are unresolvable (stale from amended/rebased commits) |
| 5 | Schema conformance | User-facing entries have `description` and `type`; type is one of `feature`/`fix`/`breaking` |
| 6 | User-facing requirement | At least one entry must be user-facing (warning in check mode, hard error during release) |
| 7 | Batch size (commits) | No single entry may reference more commits than `max_commits_per_entry` (default 5) |
| 8 | Batch size (entries) | No single commit may appear in more entries than `max_entries_per_commit` (default 5) |
| 9 | Version consistency | Project version matches across all target files |
| 10 | Changelog entry | CHANGELOG.md contains an entry for the current version |

Batch limits are configurable in `.rlsbl/config.json` under the `batch_limits` key:

```json
{
  "batch_limits": {
    "max_commits_per_entry": 5,
    "max_entries_per_commit": 5,
    "exclusions": [
      {"reason": "Large refactoring commit", "commits": ["a1b2c3d"]}
    ]
  }
}
```

Each exclusion must have a `reason` (mandatory audit trail) plus at least one of `commits` or `entries`.

## Validation cache

The `.validated` file in `.rlsbl/changes/` stores the HEAD SHA of the last successful validation run. On subsequent invocations, validation short-circuits when:

- HEAD is unchanged (same SHA)
- `unreleased.jsonl` has not been modified since the cached validation
- HEAD is a descendant of the cached SHA (verified via `git merge-base --is-ancestor`)

The cache is invalidated when:
- HEAD changes (new commits)
- `unreleased.jsonl` is modified
- A rebase makes the cached SHA no longer an ancestor of HEAD

The `.validated` file is auto-committed with an `Autogenerated: true` trailer. This commit is auto-exempted from changelog coverage requirements. It may cause `rlsbl status` to report "1 commit behind" immediately after a release — this is by design, not a bug.

## Generation

`rlsbl changelog generate` produces:
- Per-version `.md` files alongside each JSONL file
- A single `CHANGELOG.md` at the project root

The output groups entries by type under each version heading:

```markdown
## 0.28.0

Release description paragraph (from unreleased.toml).

<details><summary>Context</summary>
Optional context block (from unreleased.toml).
</details>

### Breaking
- Entry description

### Features
- Entry description

### Fixes
- Entry description
```

Versions with no user-facing entries get `- No user-facing changes.`

Versions are ordered newest first. The format is configured via `changelog_format` in `.rlsbl/config.json` (default: `"grouped"`).

Use `--dry-run` to preview the generated output without writing to disk.

## Amending released versions

`rlsbl changelog amend` modifies entries in already-released (read-only) JSONL files:

```bash
rlsbl changelog amend --version 0.25.0 --commits abc1234 --description "Fix crash on empty input" --type fix
rlsbl changelog amend --version 0.25.0 --commits def5678 --no-user-facing --no-resolve
```

The command:
1. Temporarily unlocks the target JSONL file (removes read-only flag)
2. Appends the new entry
3. Re-locks the file (chmod 444)
4. Regenerates CHANGELOG.md
5. Syncs GitHub Release notes for that version

Use `--no-resolve` to skip hash validation for old or amended commits that may no longer exist in the local history.

## Changelog discipline

Changelogs are for users, not developers. Apply this test: "Would a user who upgrades read this and think 'that affects me'?"

| Category | user_facing | type | Examples |
| --- | --- | --- | --- |
| New features, new commands, new options | `true` | `feature` | "Add `--watch` flag", "New `deploy` command" |
| Bug fixes (describe user-visible symptom) | `true` | `fix` | "Fix crash when config is empty", "Correct timeout calculation" |
| Breaking changes, API removals | `true` | `breaking` | "Remove `--force` flag", "Rename `publish` key to `pipelines`" |
| Performance improvements users notice | `true` | `feature` | "Reduce release time by 40%" |
| New install methods | `true` | `feature` | "Available via Homebrew" |
| Tests (adding, updating, reorganizing) | `false` | -- | Unit tests, integration tests, test fixtures |
| CI/CD changes | `false` | -- | GitHub Actions versions, workflow edits |
| Internal refactoring, file moves | `false` | -- | Code reorganization, module splits |
| Gitignore, dev dependencies | `false` | -- | `.gitignore` additions, dev tooling |
| Documentation (internal) | `false` | -- | Typo fixes, comment updates, internal reorg |
| Documentation (user-visible) | `true` | `feature` | New docs site, corrected install instructions |
| Dead code removal, lint fixes | `false` | -- | Unused imports, code style |

Never fabricate a user-facing entry to bypass the user-facing requirement. If a release has no user-facing changes, stop and reconsider whether a release is warranted.

## Pre-push enforcement

The `.git/hooks/pre-push` hook runs `rlsbl pre-push-check`, which:

1. Checks that every pushed commit has a JSONL entry (hard error — blocks the push)
2. Warns when a push targets a release branch but did not originate from `rlsbl release run`

Release pushes are auto-detected and exempted: when a version bump commit (message matching `^v\d+\.\d+\.\d+$`) is present in the pushed commits, the JSONL check is skipped entirely. This is safe because validation already ran during `rlsbl release run`.

The hook does not pass `$@` to rlsbl (git provides remote name and URL as args, which strictcli would reject as unknown arguments).

The release commands (`rlsbl release run`, `rlsbl release undo`) set `RLSBL_RELEASE_PUSH=1` in the push environment so the hook recognizes legitimate release pushes and suppresses the branch warning.

## Source reference

:-: ref path="rlsbl.changelog"

:-: ref path="rlsbl.changelog.validate"
