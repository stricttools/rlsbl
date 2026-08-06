# An infra bump can produce a version with no CHANGELOG heading, and then the changelog-entry check warns about the gap the release created

## Context

`bump = "infra"` is the infrastructure-only release type: it increments the
patch component and is the one bump that is *forbidden* to carry user-facing
changelog entries (`rlsbl/commands/release/__init__.py`, the infra guard that
raises `ReleaseValidationError` when `read_unreleased` finds a user-facing
entry).

At finalization, `finalize_version` (`rlsbl/changelog/files.py`) renames
`unreleased.jsonl` to `<version>.jsonl`, chmods it 0o444, and creates a fresh
empty `unreleased.jsonl`. `changelog generate` then walks
`list_versioned_files` and emits one section per versioned JSONL, reading the
version's description/context/bump from the archived `v<version>.toml`
(`_read_release_metadata_full`) and rendering `### Infrastructure` when the
bump was infra and a description exists.

Independently, `rlsbl check --tag changelog` runs `changelog-entry`
(`rlsbl/checks/changelog.py`), which greps CHANGELOG.md for a `## <current
version>` heading and warns when it is absent.

## Problem

Observed on an infra release: the per-version JSONL was written empty,
`changelog generate` emitted no `## <version>` heading for it, and rlsbl's own
`changelog-entry` check then warned that CHANGELOG.md has no entry for the
version -- a warning about a gap the release itself had just created. The tool
reports its own output as a defect, which is the worst kind of check failure:
it looks like operator error and there is no operator action that fixes it.

What is verified in the code today:

- The finalize step always creates `<version>.jsonl`, and `list_versioned_files`
  matches on the *filename*, so an empty file should still produce a section.
- `generate_version_section` with zero entries emits `## <version>` followed by
  either `### Infrastructure\n\n- <description>` (infra + description) or
  `- No user-facing changes.`

So the heading-less outcome requires one of: the version's JSONL not being
created (or being created under a name `_parse_semver` rejects), the
regeneration running before finalization and never re-running after,
CHANGELOG.md being written from a different changes dir than the one finalized
(releasable vs package home), or the archived `v<version>.toml` being missing
so `bump_type` reads empty. The batch path's per-member metadata archiving was
only recently added, which makes the last of these a live suspect for
batch-released infra versions.

**Reproduce first.** The precise trigger is not established; a fix that does
not start from a failing test would be guesswork.

## Options

**Option A -- reproduce, fix the specific path, keep the check as is.**

- Pros: smallest change; the check is right that a version with no heading is a
  defect.
- Cons: leaves the class open -- any future path that finalizes without
  regenerating reintroduces it.

**Option B -- make regeneration unconditional and terminal.**
After finalization, always regenerate CHANGELOG.md from the changes dir that
was just finalized, and assert the heading exists before the release reports
success. A release that cannot produce its own heading fails loudly at the end
instead of leaving the next `check` run to notice.

- Pros: closes the class; the release owns its own artifact; the check becomes
  a backstop rather than the discoverer.
- Cons: adds a post-finalization verification step to the release flow.

**Option C -- teach `changelog-entry` about infra releases.**
Skip or downgrade the warning when the current version's archived release file
says `bump = "infra"`.

- Pros: silences the false alarm immediately.
- Cons: wrong direction. The heading SHOULD exist for an infra release (it
  carries the `### Infrastructure` section), so suppressing the warning hides a
  real missing artifact.

Recommendation: B, with A's reproduction as its first step. Reject C.

## Affected files

- `rlsbl/changelog/files.py` -- `finalize_version`, `list_versioned_files`
- `rlsbl/changelog/generate.py` -- `generate_version_section`,
  `_read_release_metadata_full`, `generate_changelog`
- `rlsbl/commands/release/__init__.py` -- the infra guard and the finalize step
- `rlsbl/commands/monorepo/batch_release.py` -- per-member metadata archiving
- `rlsbl/checks/changelog.py` -- `check_changelog_entry`
- Tests: `tests/test_changelog_generate.py`, `tests/test_changelog_integration.py`

## Effort

Medium: the reproduction is the work. Once a failing fixture exists (an infra
release with an empty unreleased.jsonl, run through both the standalone and the
batch paths), the fix and the post-finalization assertion are small.
