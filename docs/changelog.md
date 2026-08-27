---
description: "JSONL changelog reference: entry schema, the format_version gate, adding entries and the auto-commit's HEAD move, validation, caching, generation."
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
- Unlike the 444-immutable `.jsonl` files, the per-version `.md` files are writable and regenerated in place on every release. If a `.md` file has been deliberately locked read-only, generation is handled gracefully: an idempotent, compare-first atomic writer skips the write when the content is already identical and never errors on a locked-but-current file
- `CHANGELOG.md` at the project root aggregates all versions (newest first)

## Entry schema

Each line in a JSONL file is a self-contained JSON object. Every line rlsbl writes carries a leading `format_version` (the per-line gate) plus 2-4 content fields depending on whether the change is user-facing. User-facing entries require a description and type for generated output; non-user-facing entries need only commit hashes and the boolean flag:

```jsonl
{"format_version":1,"commits":["a1b2c3d"],"user_facing":true,"description":"**New feature.** What it does for users.","type":"feature"}
{"format_version":1,"commits":["e4f5g6h","i7j8k9l"],"user_facing":false}
```

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `format_version` | Always | integer | Per-line format gate (currently `1`). Validated by strictspec; stamped automatically on write. |
| `commits` | Always | array of strings | Commit hashes (any prefix length — resolved to full SHA at validation) |
| `user_facing` | Always | boolean | Whether this change affects users |
| `description` | If user_facing | string | One-line description, supports markdown (bold, backticks) |
| `type` | If user_facing | string | One of: `feature`, `fix`, `breaking` |

Non-user-facing entries need only `format_version`, `commits`, and `user_facing: false`. They exist solely for commit coverage and are excluded from generated output.

## The format_version gate

The document shape of one JSONL line — the per-line `format_version` gate plus the field/enum/conditional-required shape — is validated by the **strictspec-generated validator** (`rlsbl/strictspec_gen/changelog_entry_commit_validator.py`). strictspec is the single document authority for shape; rlsbl keeps only what strictspec cannot see native (hash resolution, tag ranges, coverage vs git, batch limits, cross-file rules).

Reading is **explicit two-mode**, never a silent fallback:

- A line **carrying** `format_version` is validated via strictspec. A missing gate is fine only in legacy mode; a *wrong* or unsupported `format_version` is always a hard error.
- A line **lacking** `format_version` is a **legacy** line. It is accepted only when the reader is in legacy mode. The mode is chosen explicitly per repo by the `changelog_format_version_enforced` config key.

### Transition (`changelog_format_version_enforced`)

Historical finalized `x.y.z.jsonl` files and in-flight `unreleased.jsonl` lines predate the gate and carry no `format_version`. Because retroactively stamping every line across all repositories would be disruptive, the rollout is incremental: each repo transitions independently by setting the `changelog_format_version_enforced` config key, which controls both reading behavior and check severity as shown below:

| `changelog_format_version_enforced` | Reading | Checks |
| --- | --- | --- |
| key absent | legacy (unstamped lines tolerated) | `changelog-format-version` **warns**: "enforcement not yet enabled" |
| `true` | enforced (every line must carry `format_version`) | `changelog-format-version-gate` **errors** on any unstamped/wrong-version line |
| `false` | legacy (explicit opt-out) | no warning; gate check skipped |

There is **no enforced default**: absence is legacy *and* is surfaced as visible pressure by the warn check — never silent. Every line `rlsbl changelog add` writes carries `format_version`, so a repo that has been on the current tooling for a full release cycle is already fully stamped. To enable enforcement, confirm every line in the changes dir carries the field and set:

```jsonc
// .rlsbl/config.json
"changelog_format_version_enforced": true
```

The one-time bootstrap stamper that seeded historical lines has been retired — the fleet is stamped and new lines stamp themselves. Once the key is `true`, any line still lacking `format_version` hard-fails the `changelog-format-version-gate` check; the remediation is to add `"format_version":1` to that line (finalized `x.y.z.jsonl` files are read-only 444, so unlock, edit, and re-lock), or to record `false` and stay in legacy mode deliberately.

### Multiplicity rules

- Multiple commits can map to one entry (one feature spanning several commits)
- One commit can appear in multiple entries (a commit that fixes a bug and adds a feature)
- The coverage check only requires that every commit appears in at least one entry

## Adding entries

Use `rlsbl changelog add` after each commit or group of related commits to append a structured entry to `unreleased.jsonl`. Each entry links one or more commit hashes to a description and type, and the command auto-commits the JSONL file unless `--no-auto-commit` is passed:

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
| `--no-auto-commit` | Skip the auto-commit of `unreleased.jsonl`. |

The command:
1. Resolves each hash via `git rev-parse` (errors on invalid hashes)
2. In monorepo mode, validates that each commit touches files within the current sub-project's path/watch patterns
3. Validates entry schema
4. Appends to `unreleased.jsonl`
5. Auto-commits the file (message: `changelog: <description>`) unless `--no-auto-commit` is passed

### The auto-commit moves HEAD

Step 5 creates a commit, so `HEAD` after `changelog add` is the changelog
commit, not the code commit you were working on. That composes badly with
multi-step sessions where a code commit is still being shaped:

```bash
git commit -m "fix the parser"
rlsbl changelog add --commits HEAD --description "..." --type fix
git commit --amend                 # amends the CHANGELOG commit, not the fix
```

The amend lands on the wrong commit and has to be unwound by hand. Two ways to
avoid it, both explicit:

- **Finish the code commit first.** Treat `changelog add` as the last step for
  a change, after any amends or rebases. This is the normal flow, and it is
  also why entries reference hashes: once recorded, the commit should not move.
- **Defer the commit** with `rlsbl changelog add --no-auto-commit`, keep working,
  and commit `unreleased.jsonl` yourself when the code commit is settled.

If a code commit does move after its entry was recorded, the hash-resolution
and orphan-detection checks catch the stale hash loudly (see Validation below);
`rlsbl changelog edit` repoints the entry at the new hash.

## Validation

`rlsbl check --tag changelog` runs the changelog checks: hash resolution, commit coverage, schema conformance, batch size limits, and the per-line `format_version` gate. All error-severity checks must pass before a release proceeds. Failed checks produce specific error messages identifying the exact entry or commit that caused the failure:

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
| 11 | format_version (warn) | `changelog-format-version`: warns while `changelog_format_version_enforced` is absent (enforcement not yet enabled) |
| 12 | format_version gate | `changelog-format-version-gate`: once enforcement is on, every JSONL line must carry a valid `format_version` |

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

The `.validated` file in `.rlsbl/changes/` stores the HEAD SHA of the last successful validation run, enabling incremental validation that avoids re-checking unchanged state across repeated invocations. This caching reduces validation time from seconds to near-instant for projects with large commit histories. On subsequent invocations, validation short-circuits when:

- HEAD is unchanged (same SHA)
- `unreleased.jsonl` has not been modified since the cached validation
- HEAD is a descendant of the cached SHA (verified via `git merge-base --is-ancestor`)

The cache is invalidated when:
- HEAD changes (new commits)
- `unreleased.jsonl` is modified
- A rebase makes the cached SHA no longer an ancestor of HEAD

The `.validated` file is auto-committed with an `Autogenerated: true` trailer. This commit is auto-exempted from changelog coverage requirements. It may cause `rlsbl status` to report "1 commit behind" immediately after a release — this is by design, not a bug.

## Generation

`rlsbl changelog generate` reads all JSONL files in `.rlsbl/changes/` (sorted by semver, newest first), groups user-facing entries by type (breaking, features, fixes), and produces a fully formatted CHANGELOG.md plus per-version markdown files. This command is idempotent and safe to run at any time -- it overwrites existing output with freshly generated content, so manual edits to CHANGELOG.md are always lost on the next run. It produces:
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

`rlsbl changelog amend` modifies entries in already-released (read-only, chmod 444) JSONL files, allowing corrections to descriptions, types, or commit references after a version has shipped. The command handles file locking, changelog regeneration, and GitHub Release note synchronization automatically:

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

## Modifying existing entries

`rlsbl changelog edit` modifies an existing entry in any JSONL file (unreleased or released) by looking it up via commit hash. Instead of appending a new entry like `add` or `amend`, it performs a partial field update on the matched entry and rewrites the file atomically. This is the correct tool when an entry already exists but has the wrong type, description, or user-facing status.

The command declares itself as an update: the resource is `changelog-entry`, the write mode is sparse, `--commits` and `--id` are the two ways to identify the instance, and `--description`, `--type` and `--user-facing` are the properties. Three rules follow from that declaration rather than from anything the command checks itself:

- **A property you do not supply is untouched.** That is what sparse means, and it is why none of the three carries a default.
- **At least one property is required.** Naming an entry and changing nothing is refused before the command runs: `update "changelog-entry": at least one property is required: --description, --type, --user-facing`.
- **`--description` and `--type` can be cleared.** Both are nullable, so `--unset-description` and `--unset-type` exist and remove the field rather than writing an empty string to it. A cleared property is a write, so it satisfies the at-least-one rule; supplying a value and its unset together is a parse error.

Naming no entry at all is refused too, by the `entry-selection` constraint: at least one of `--commits`, `--id`.

The following table distinguishes the four commands that interact with changelog entries and GitHub Release notes:

| Command | What it does | Target file | Use case |
| --- | --- | --- | --- |
| `changelog add` | Append new entry | `unreleased.jsonl` | Document new work |
| `changelog amend` | Append new entry | Released `.jsonl` (read-only, temporarily unlocked) | Add missing coverage to a past release |
| `changelog edit` | Modify existing entry in-place | Any `.jsonl` (auto-detected by commit hash) | Fix wrong type, description, or user-facing status |
| `release edit` | Sync GitHub Release notes from CHANGELOG.md | GitHub Release | Update release notes after CHANGELOG.md changes |

## Changelog discipline

Changelogs are for users, not developers. Apply this test: "Would a user who upgrades read this and think 'that affects me'?" If the answer is no, the entry should be marked `--no-user-facing`. The table below classifies common change types to guide consistent categorization across releases:

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

The `.git/hooks/pre-push` hook captures git's push refs and runs `rlsbl check --tag prepush` on every branch push, to verify changelog completeness before commits reach the remote. This is a hard enforcement point that blocks pushes with uncovered commits, preventing incomplete changelogs from reaching the main branch:

1. Checks that every pushed commit has a JSONL entry (hard error — blocks the push)
2. Hard-errors when a push targets a release branch but did not originate from `rlsbl release run`

The hook is namespace-aware. It reads git's stdin (one line per pushed ref) and only enforces on `refs/heads/*`:

| Ref namespace | Behavior |
| --- | --- |
| `refs/heads/*` | Enforce all prepush checks |
| `refs/tags/*` | Exit 0 — release tags are pushed by rlsbl's own release flow |
| `refs/backups/*` | Exit 0 — tool-owned backup slots |

Release pushes are auto-detected and exempted: when a version bump commit (message matching `^v\d+\.\d+\.\d+$`) is present in the pushed commits, the JSONL check is skipped entirely. This is safe because validation already ran during `rlsbl release run`.

The hook does not pass `$@` to rlsbl (git provides remote name and URL as args, which strictcli would reject as unknown arguments).

There is no environment-variable bypass. Release-internal pushes run `git push --no-verify`, so they never invoke the hook at all — which means any push that DOES reach the manual-push check is by construction a hand-made one, and is rejected.

## Examples

### Typical workflow after committing changes

```bash
# After implementing a feature across two commits
git log --oneline -3
#   f9a1b2c Add --format flag to graph command
#   d4e5f6a Refactor graph builder internals
#   a7b8c9d (tag: v0.5.0) v0.5.0

# Cover the user-facing commit
rlsbl changelog add --commits f9a1b2c --description "Add `--format` flag to `monorepo graph` command" --type feature

# Cover the internal refactor
rlsbl changelog add --commits d4e5f6a --no-user-facing

# Verify coverage
rlsbl check --tag changelog
#   changelog-hashes .............. pass
#   changelog-range ............... pass
#   changelog-coverage ............ pass
#   changelog-orphans ............. pass
#   changelog-schema .............. pass
#   changelog-user-facing ......... pass
#   changelog-batch-commits ....... pass
#   changelog-batch-entries ....... pass
#   changelog-entry ............... warn  CHANGELOG.md missing entry for 0.5.0
```

### Multi-commit feature entry

When a single feature spans multiple commits, group them in one changelog entry rather than creating separate entries for each commit. This preserves the semantic connection between related changes and makes the generated CHANGELOG.md readable for users who want to understand what changed at a feature level rather than at a commit level:

```bash
rlsbl changelog add \
  --commits a1b2c3d,e4f5g6h,i7j8k9l \
  --description "Add health check support to deploy targets" \
  --type feature
```

### Fixing a wrong entry

If an entry has the wrong type or description, use `changelog edit` to modify it in place. The command finds the entry by commit hash or entry ID, applies the field changes you specify (such as type, description, or user-facing status), and rewrites the JSONL file atomically without disturbing other entries:

```bash
# Find the entry by commit hash and change its type from feature to fix
rlsbl changelog edit --commits a1b2c3d --type fix --description "Fix deploy crash when health check URL is unreachable"

# Flip an entry to internal and drop the description it no longer needs
rlsbl changelog edit --id 18c9... --no-user-facing --unset-description --unset-type
```

### Amending a released version

Released JSONL files are normally read-only (chmod 444) and immutable, but `changelog amend` temporarily unlocks the target file, appends a new entry, re-locks it, regenerates CHANGELOG.md with the updated content, and syncs the corresponding GitHub Release notes. Use this when a commit was missed during the original release or when a correction needs to be backported to an older version's changelog:

```bash
rlsbl changelog amend --version 0.4.0 --commits abc1234 --description "Fix race condition in parallel watch" --type fix
```

### What unreleased.jsonl looks like

```jsonl
{"format_version":1,"id":"01J5K...","commits":["f9a1b2c"],"user_facing":true,"description":"Add `--format` flag to `monorepo graph` command","type":"feature"}
{"format_version":1,"id":"01J5K...","commits":["d4e5f6a"],"user_facing":false}
```

### Generated CHANGELOG.md output

The `rlsbl changelog generate` command reads all `.rlsbl/changes/*.jsonl` files in reverse semver order and produces a single CHANGELOG.md at the project root, plus per-version `.md` files alongside each JSONL file. User-facing entries are grouped by type under Breaking, Features, and Fixes sub-headers. Versions with no user-facing entries show a placeholder note. Example output after running the command:

```markdown
<!-- Generated by rlsbl from .rlsbl/changes/ -- do not edit -->

## 0.6.0

Add graph formatting and deploy improvements.

### Features
- Add `--format` flag to `monorepo graph` command
- Add health check support to deploy targets

### Fixes
- Fix deploy crash when health check URL is unreachable

## 0.5.0

- No user-facing changes.
```

## Source reference

The changelog system is implemented across several modules handling file I/O, JSONL schema validation, hash resolution via git rev-parse, and CHANGELOG.md generation from structured entries. These modules are the authoritative source for validation rules, entry formats, and generation logic.

:-: ref path="rlsbl.changelog"

:-: ref path="rlsbl.changelog.validate"
