# Rename "implicit mode" / "explicit mode" to descriptive names

## Context

Monorepo workspaces operate in one of two release models, currently named by
whether `[[releasables]]` is present in `workspace.toml`:

- **"implicit mode"** (no `[[releasables]]`): each project is independently
  versioned via its own `<path>/.rlsbl/` (config, changes/, version), released
  via `[packages.<name>]` sections in the release file.
- **"explicit mode"** (`[[releasables]]` present): packages are grouped into
  releasable units sharing one version, changelog, and tag under
  `.rlsbl-monorepo/releasables/<name>/`, released via `[releasables.<name>]`
  sections.

## Problem

The names describe a syntactic accident (whether a TOML table was written)
rather than what each model IS. "Implicit" doesn't say what is implicit — and
the mode isn't implicit about anything meaningful: each package explicitly owns
its own release state. If anything the names are backwards: "implicit" mode is
the more explicit-per-package arrangement, while "explicit" mode consolidates
state away from packages.

This repeats the naming lesson already learned with `internal` →
`changelog_exempt` → `dev_node`: the name should describe what the thing is
(its role/structure), not the mechanism that selects it. A reader seeing
"requires the workspace to be in explicit mode" in CLI help has no way to guess
what that means without reading the docs.

## Proposed names

| Current | What it actually is | Proposed |
|---|---|---|
| implicit mode | release state lives per-package (`<path>/.rlsbl/`) | **per-package mode** |
| explicit mode | release state consolidated per releasable group | **releasable mode** |

"releasable mode" reuses the noun the config already uses (`[[releasables]]`,
`releasables/` directory, `--releasable` flag) instead of introducing a third
vocabulary.

Alternatives considered:

- "standalone mode" / "grouped mode" — also descriptive, but "standalone" is
  overloaded (already means non-monorepo repos with a root `.rlsbl/`).
- Keeping "explicit" for the releasable side only — halves the fix but leaves
  an unpaired term; per-package mode still needs a real name, and the pair
  should read as a pair.

## Affected surfaces (verified occurrences; do a full grep before implementing)

- `rlsbl/workspace.py` — `is_explicit_mode()` predicate (rename, e.g.
  `is_releasable_mode()`; update all call sites).
- Call sites / comments using the terms: `rlsbl/checks/_common.py` (lines ~24,
  31, 80, 87, 106-117, 167, 180-185), `rlsbl/commands/release_retry.py`
  (~166-167), `rlsbl/__init__.py` (~186-192, 1479-1495), `rlsbl/tag_glob.py`
  (~26).
- CLI help text: `migrate-releasable` help string ("Requires the workspace to
  be in explicit mode...") in `rlsbl/__init__.py` (~1495), surfaced in
  `docs/cli-monorepo.md:184`.
- Generated docs (`docs/`) regenerate via selfdoc after source changes.
- The rlsbl section of the workspace-level `~/Projects/CLAUDE.md` uses
  "implicit mode"/"explicit mode" in several places — update after release so
  all sessions pick up the new vocabulary.
- Additional files matched a case-insensitive grep for "implicit" and need
  triage (some hits may be unrelated uses of the word): `action_versions.py`,
  `member_context.py`, `release_file.py`, `commands/changelog_cmd.py`,
  `commands/monorepo/batch_plan.py`, `commands/monorepo/commands.py`,
  `commands/monorepo/batch_release_init.py`, `commands/release/*.py`,
  `commands/init_cmd.py`, `checks/project.py`, `checks/changelog.py`,
  `changelog/home.py`, `docs/rlsbl-changelog-home.md`,
  `docs/import-scanning.md`.

## Solution sketch

1. Full grep for `implicit`, `explicit_mode`, `explicit mode` across code,
   docs templates, CLI help, tests; triage true positives.
2. Rename the predicate and terms in one pass (collapse-multi-pass rule: rename
   function + call sites + comments + help text + tests per file group in a
   single sweep).
3. Regenerate selfdoc docs; update the workspace-level CLAUDE.md section after
   the release ships.
4. Internal-only rename (function names, comments, help text) — no config file
   format changes, so no migration needed. If any public-facing docs teach the
   old terms, note the rename in the changelog entry (user-facing: docs/help
   wording change).

## Effort estimate

Low-Medium. Mostly mechanical rename + doc regeneration; the only care point
is triaging incidental uses of the word "implicit" and keeping help-text
wording coherent. No behavior change, no config migration.
