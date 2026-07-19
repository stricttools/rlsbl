# Monorepo absorb: migration gaps for absorbing released standalone projects

Filed 2026-07-19. Source: a design review of a planned multi-repo → monorepo migration
(several mature, registry-published, rlsbl-managed standalone repos being absorbed into
one implicit-mode workspace). The review surfaced five tool-side gaps in the absorb /
monorepo flow. They are grouped here because they share one theme: **absorb works for
young projects but has unresolved edges for projects with real release history.**

Items 1 and 2 are correctness gaps; 3 and 4 are ergonomics/derisking; 5 needs
verification before any work.

---

## 1. No tags imported → no changelog-coverage anchor for an absorbed package's first monorepo release

### Problem

`monorepo absorb` grafts history via `git subtree add` but imports none of the source
repo's `vX.Y.Z` tags (no code path in `rlsbl/commands/monorepo/extract.py` touches
tags). The changelog coverage range is tag-anchored (`<last_version_tag>..HEAD` via
`git describe --match`). Consequence: an absorbed package that is already at, say,
version 0.23.x arrives with its full grafted history and **no anchor tag**, so at its
first monorepo release the "unreleased range" is unbounded or ambiguous. It is not
defined whether the release flow treats this as "never tagged → first release, publish
as-is" (and if so, whether coverage checks then demand JSONL entries for the entire
grafted history) or something else. Whatever the current behavior is, it is emergent,
not designed.

### Solutions

- **A. Absorb-time anchor record.** Absorb records the boundary (the subtree merge
  commit, or the source HEAD) plus the source's current version in the workspace/project
  state; coverage and release treat that commit as the "last release" anchor. Pros:
  cheap, no tag pollution, works even when source tags are missing. Cons: a second
  anchoring mechanism next to tags; checks must consult it.
- **B. Optional tag import.** `absorb --import-tags` recreates the source's version tags
  at the corresponding grafted commits under the monorepo scheme (prefixed/path-style).
  Pros: one anchoring mechanism (tags), full in-repo version archaeology. Cons: commit
  mapping across the subtree graft is nontrivial; many tags; pushes them on next release.
- **C. Document + first-release carve-out.** Explicitly define: absorbed package with a
  never-tagged current version ⇒ first release publishes as-is and coverage starts at
  the absorb boundary, implemented as a documented special case. Pros: smallest change.
  Cons: boundary detection still needs *some* recorded anchor (subsumes a lite form
  of A).

Recommendation: A (possibly with B as an opt-in later). Whatever is chosen, add an
integration test: absorb a fixture repo with released versions + changelog history, then
run a release and assert coverage checks pass without hand-fixups.

## 2. Absorb changelog migration in implicit mode: possible double-append, and `monorepo cleanup` refuses to help

### Problem

The subtree copy already carries the source's `.rlsbl/changes/` under the new package
dir. Absorb *additionally* migrates changelog entries from the source: with
`--releasable` they go to the releasable's changes dir, without it they are appended
into `<package>/.rlsbl/changes/` — which the subtree just populated with the same
content. In implicit mode this looks like it appends entries into files that already
contain them (or creates same-named files alongside). Meanwhile `monorepo cleanup`,
the residue-removal tool, requires an explicit-mode workspace and hard-errors in
implicit mode — so even if duplication occurs, the blessed cleanup path is unavailable
exactly where it would be needed.

### Solutions

- **A. Make implicit-mode migration a verified no-op.** If the migration target equals
  the subtree-copied dir and content is identical, skip with a log line. Add a red-green
  test absorbing a fixture with populated changes/ in implicit mode and asserting no
  duplicate JSONL lines/files.
- **B. Support implicit mode in cleanup.** Teach `monorepo cleanup` an implicit-mode
  path (dedup within per-package changes/). Cons: treats the symptom.
- **C. Both**: fix the append at the source (A) and keep cleanup explicit-only.

Recommendation: A. First step is simply writing the failing test — the exact current
behavior is unverified.

## 3. Absorb conflates the subtree prefix with the workspace name

### Problem

`absorb <package_name> <source_path>` uses the positional arg as both the directory
prefix and the workspace entry's `name` (entry written as path = name = arg). Layouts
where the directory is neutral/descriptive but the package keeps its branded registry
identity (fields that already exist: `name`, `registry_name`, and tag format derives
from `name`) require hand-editing workspace.toml immediately after every absorb.

### Solutions

- **A. Add `--name` and `--registry-name` flags to absorb** (positional stays the
  prefix/path). Pros: declarative, matches existing workspace.toml fields; consistent
  with the "verbose explicit invocations" tool philosophy. Cons: none notable.
- **B. Status quo + documented post-edit.** Pros: nothing to build. Cons: every absorb
  of this shape needs a manual follow-up edit that affects tag naming if forgotten —
  easy for an agent to miss, which is exactly what the hard-constraint philosophy says
  to prevent.

Recommendation: A, with `monorepo add` gaining the same flags for parity.

## 4. Dual-registry (npm + PyPI) single package in a monorepo: supported in code, proven nowhere

### Problem

The inlined publish router emits one publish job per detected target, so a package dir
containing both a pyproject and a package.json gets both a PyPI and an npm job keyed on
the same tag. No workspace in the fleet exercises this; the sandbox has no fixture for
it. Projects that publish a Python CLI plus a thin npm shim from one package dir will
hit this path blind.

### Solutions

- **A. Sandbox fixture + e2e test**: a dual-target package in the publish-gating
  sandbox monorepo, asserting the router generates and gates both jobs from one tag.
- **B. Decide against it** and document "one registry per package dir; use two dirs"
  (the pattern existing monorepos use), with a check that errors on dual-manifest dirs.

Recommendation: A if dual-target is meant to be supported; B if not. Either is better
than the current undefined middle.

## 5. (Verify first) CI scaffold support for service containers

### Problem (suspected)

Per-package CI templates may have no way to declare service containers (e.g. Postgres
for a package whose test suite needs a live database). If so, DB-backed packages in a
monorepo can only have degraded CI (skipped DB tests), which contradicts honest-CI
expectations.

### Next step

Verify before designing: check whether any current template/config path can express
services, and how existing DB-using projects' CI actually runs their suites. If the gap
is real, design a config key (e.g. per-pipeline `services`) that the scaffold renders
into the workflow. If templates already support it, close this item with a doc pointer.

---

## Affected files (by area)

- Absorb: `rlsbl/commands/monorepo/extract.py` (absorb + `_migrate_changelog_from_source`,
  `validate_absorb_preconditions`), `rlsbl/commands/monorepo/commands.py` (`add` parity),
  `rlsbl/workspace_types.py` (name/registry_name fields)
- Coverage/anchoring: changelog range resolution + checks (`rlsbl/checks/`), release
  preflight
- Cleanup: `rlsbl/commands/monorepo/` cleanup implementation
- Publish router: `rlsbl/commands/monorepo/publish_inline.py`
- CI templates: `rlsbl/templates/`
- Tests/fixtures: absorb fixtures with released history; sandbox dual-target fixture

## Effort estimate

- Item 1: medium (design + implementation + integration test)
- Item 2: small-medium (test first; likely a targeted fix)
- Item 3: small
- Item 4: small (fixture + assertion) or trivial (document + check)
- Item 5: trivial to verify; small-medium if the gap is real

Independent of each other; can be picked up piecemeal (split this file if triaged
separately).
