# `monorepo status`'s Unreleased column says "entries" but does not count entries

## Context

`rlsbl monorepo status` prints an `Unreleased` column for every project (and,
in explicit mode, every releasable). Both renderers derive it the same way
(`rlsbl/commands/monorepo/commands.py`): open CHANGELOG.md, find the `##
<latest tag version>` heading, and count lines starting with `- ` above it
(falling back to counting every bullet in the file when the tag's heading is
absent or there is no tag at all). The number is then rendered as `0`,
`1 entry`, or `N entries`.

The standalone `rlsbl status` answers the same question from a different
source: it reports JSONL coverage as `X/Y commits covered`, computed from
`unreleased.jsonl` against the unreleased commit range.

## Problem

The column is labelled in units it does not measure, and the mismatch is not
cosmetic -- it changes what an operator concludes:

- It counts **CHANGELOG.md bullet lines**, not JSONL entries. A single JSONL
  entry can render as one bullet, and a version section with no user-facing
  entries renders `- No user-facing changes.` -- itself a bullet, counted.
- Unreleased work does not live in CHANGELOG.md at all: entries sit in
  `unreleased.jsonl` until a release finalizes them. So the "unreleased" count
  is really "bullets belonging to version sections newer than the latest tag",
  which is normally zero even when there are dozens of unreleased entries.
- The fallback paths make it worse: with no tag, or with a tag whose heading is
  missing from CHANGELOG.md, it counts *every bullet in the entire file* -- the
  project's whole release history -- and labels that "N entries" unreleased.
- Whatever the number means, "entries" is the wrong noun. Reported observation
  is that operators read it as a commit count; the code cannot produce a commit
  count from any path, which is itself the evidence that the label communicates
  something the implementation never computes.

## Options

**Option A -- count JSONL entries and keep the label.**
Read `unreleased.jsonl` through the changelog home resolver (releasable-aware,
which the column already does for the CHANGELOG path) and count entries.

- Pros: the column finally means what it says; consistent with `changelog add`,
  which is where entries come from.
- Cons: does not tell an operator whether the entries *cover* the unreleased
  commits, which is the question a release actually gates on.

**Option B -- report coverage, matching `rlsbl status`.**
Render `X/Y` (covered commits / unreleased commits) per project, exactly as the
standalone status command does, and rename the header to `Coverage`.

- Pros: one vocabulary across both status commands; answers the question that
  blocks a release; makes an uncovered commit visible workspace-wide, which is
  the whole point of a workspace status table.
- Cons: needs a per-project unreleased range (tag glob resolution per row --
  already done in both renderers for the Tag column, so the machinery is
  present) and is more work per row than reading one file.

**Option C -- keep the bullet count, rename the header to what it is.**
`Unreleased` becomes something like `New bullets`.

- Pros: trivially honest.
- Cons: keeps a metric nobody wants and that is zero in the normal case.

Recommendation: B. The two status commands should answer the same question the
same way, and coverage is the number that decides whether a release can run.

## Affected files

- `rlsbl/commands/monorepo/commands.py` -- `_count_unreleased_from_changelog`,
  `_cmd_status_explicit`, `_cmd_status` (the inline duplicate of the same
  counting logic; the two copies should collapse into one helper as part of
  whichever option lands)
- `rlsbl/commands/status.py` -- the coverage computation to reuse for Option B
- `rlsbl/tag_glob.py` -- `resolve_monorepo_tag_glob`, already called per row
- Tests: `tests/test_monorepo_status.py`

## Effort

Small for A or C; medium for B (per-row range resolution plus fixtures for
tagged, untagged, and releasable-member projects).
