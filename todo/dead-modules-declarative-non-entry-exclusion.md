# dead-modules: declarative known-non-entry-files exclusion

## Context

The `dead-modules` quality check (`rlsbl check --tag quality` / `--all`) walks a
project's source files, builds a file-level import graph, and warns about any
"production" source file that is not reachable from a declared entry point.

For npm projects the entry points come from `package.json` (`exports`, `main`,
`bin`); production files are every `.js`/`.ts`/`.mjs`/`.cjs`/`.tsx` file that is
not filtered out by the check's built-in exclusion categories.

The exclusion categories are currently **hardcoded** and non-configurable:

- `_is_test_context()` in `rlsbl/import_scanners.py` -- `__tests__/`, `testdata/`
  at any depth; `test/`, `tests/`, `example/`, `examples/`, `integration_test/`
  as first path component; and the `_TEST_FILE_PATTERNS` basename patterns
  (`*.test.[jt]sx?`, `*.spec.[jt]sx?`, `test_*.py`, `*_test.go`, etc.).
- `_ROOT_NON_MODULE_DIRS = {"scripts"}` in `rlsbl/dep_validation.py`.
- `_is_inside_python_package()` (JS/TS files that are Python data resources).
- Browser asset directories (`static`, `public`, `assets`) per docs.

The only per-invocation exclusion input is `exclude_dirs`, and that is populated
**only** for monorepo/workspace contexts (sibling project directories) --
standalone projects always pass `exclude=None`
(`rlsbl/checks/quality.py`, `check_dead_modules`). The per-project
`.rlsbl/lint/<lang>.toml` files (`[files] exclude`, `[entry-point] ignore`) are
consumed exclusively by the **library-lint** path (`lint_library` ->
`rlsbl/lint/config.py`) and are never read by the dead-modules detector.

## Problem

Some source files are legitimately, permanently unreachable from any package
entry point by design, yet are neither test files nor located in an
excluded directory. Two common shapes:

1. **Demo / gallery apps** -- a top-level directory (not named
   `example`/`examples`) holding a standalone browser demo whose script is not
   part of the published API and is not imported by anything.
2. **Test-runner / tooling config files at the project root** -- e.g.
   `playwright.config.js`, `vitest.config.js`, `jest.config.js`,
   `eslint.config.js`, `vite.config.js`. These are consumed by their respective
   tool runners, never imported by application code, and their basenames do not
   match the `*.test.*` / `*.spec.*` patterns, so the check does not recognize
   them as non-production.

Concretely, a consumer project with a demo app and two test-runner configs gets
a persistent `dead-modules` WARN flagging exactly those three files. The warning
is pure noise -- the files are correctly unreachable -- but there is **no
sanctioned way to silence it**. The project owner is left with only
illegitimate workarounds that the guardrail is meant to forbid: adding a fake
import, renaming a file into a `*.spec.*` shape, or laundering the file into
`package.json`'s `bin`/`exports` to fake an entry point. Each of those corrupts
real signal to quiet a benign warning.

This is a gap in the hard-constraint philosophy: the check offers no
declarative, reviewable escape valve, so users are pushed toward exactly the
hacks the check exists to discourage.

## Solutions

### Option A (recommended): declarative `known_non_entry_files` list

Add a committed, reviewable per-project list of files that are known to be
non-entry, non-imported by design. A file on the list is removed from the
dead-module *candidate* set (it can never be reported as dead), but it is NOT
treated as an entry point (it does not seed BFS and cannot make other files
reachable). This keeps it a pure suppression of the file itself, not an
entry-point laundering vector.

Design points aligned with the project's philosophy:

- **A committed list, not a bypass flag.** No `--skip-dead-modules` /
  `--ignore-warnings`. The exclusion lives in a file under version control so it
  is diffable and reviewable in PRs.
- **Each entry requires a `reason`.** Mirror the existing `dep-overrides.toml`
  pattern (`load_dep_overrides` in `dep_validation.py`), which already forces a
  non-empty `reason` string per entry and hard-errors otherwise. This forces the
  author to justify each suppression.
- **Exact-path matching, not globs (or globs opt-in).** Prefer literal
  project-relative paths so the list cannot silently grow to cover files added
  later. If globbing is allowed, keep it explicit and narrow.
- **Stale-entry detection.** Add a check (or fold into `dead-modules`) that
  hard-errors when a `known_non_entry_files` entry points at a path that no
  longer exists, so the list cannot rot. This matches the "no dead config"
  discipline used elsewhere.

Placement options for the list (pick one during design):

- New top-level file `.rlsbl/dead-modules.toml` (parallels `dep-overrides.toml`).
- A `[dead_modules] known_non_entry_files = [...]` table in `.rlsbl/config.json`
  (JSON, so the per-entry-reason requirement would need an array of objects).
- A `[dead-modules]` section in the per-language `.rlsbl/lint/<lang>.toml` files
  (co-locates with other file-level lint config, but those are currently only
  read by library-lint, so the loader would need to also feed dead-modules).

Pros: eliminates the incentive to hack; auditable; reason-gated; extends an
existing, proven pattern (`dep-overrides.toml`).
Cons: new config surface + loader + wiring through `check_dead_modules` for both
standalone and workspace contexts; needs stale-entry guard to avoid rot.

### Option B: auto-recognize well-known root tool-config basenames

Extend the built-in non-production classification to treat a curated set of
root-level tool-config basenames as non-production
(`*.config.{js,ts,mjs,cjs}` at the project root, or an explicit allowlist of
`playwright.config.*`, `vitest.config.*`, `jest.config.*`, `vite.config.*`,
`eslint.config.*`, etc.).

Pros: zero per-project config; fixes the most common case (root configs) for
everyone automatically.
Cons: does not cover the demo/gallery-app case (arbitrary directory names);
hardcoded allowlist needs maintenance as tools appear; a too-broad
`*.config.*` rule could mask a genuinely dead config. Best combined with Option
A rather than as a replacement.

### Option C: recognize a conventional demo directory name

Add `demo`/`demos`/`gallery` (or a configurable set) to the root-relative
non-production directory list alongside `example`/`examples`.

Pros: trivial; covers the demo-app shape.
Cons: still hardcoded and English-name-specific; does not cover root config
files; a blanket directory-name convention is less precise than an explicit
file list. Weaker than Option A.

### Recommendation

Ship **Option A** as the primary mechanism (declarative, reason-gated,
reviewable, with stale-entry detection). Optionally layer **Option B** for the
extremely common root tool-config case so most projects need no config at all.
Avoid relying on Option C alone.

## Affected files

- `rlsbl/dep_validation.py` -- `find_dead_npm_modules` (and the sibling
  `find_dead_go_packages`, `find_dead_dart_modules`, `find_dead_jvm_modules`,
  `find_dead_modules`) to subtract the known-non-entry set from the candidate
  files; possibly a new `load_dead_module_overrides()` loader modeled on
  `load_dep_overrides()`.
- `rlsbl/checks/quality.py` -- `check_dead_modules` to load the per-project list
  and pass it through for both standalone and workspace contexts.
- `rlsbl/import_scanners.py` -- if Option B/C chosen, extend `_is_test_context`
  layers / add a tool-config or demo-dir classifier.
- Config loader for the chosen placement (`.rlsbl/config.json` schema,
  `rlsbl/lint/config.py`, or a new `.rlsbl/dead-modules.toml` reader).
- A new stale-entry check registration (checks.toml + a check function) if the
  list gets a rot guard.
- `rlsbl/data/checks.toml` -- if a companion stale-entry check is added.
- `docs/dep-validation.md` and `docs/import-scanning.md` -- document the new
  exclusion mechanism under the dead-modules "Exclusions" section.
- Tests under `tests/` -- cover: list suppresses a flagged file; a listed file
  does NOT become an entry point (does not make other files reachable);
  reason-required hard error; stale-entry hard error.

## Effort

Medium. The detection change itself is small (subtract a set before computing
dead files). The bulk is the new config surface + loader + wiring through both
standalone and workspace code paths, the reason-required validation, the
stale-entry guard, docs, and a focused test suite. Estimate ~0.5-1 day
including tests and docs. Option B alone (root tool-config recognition) is a
couple of hours but is only a partial fix.
