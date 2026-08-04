# `dead-modules` reports every TypeScript source as dead when package.json points at build output

## Context

The `dead-modules` check (`rlsbl/checks/quality.py`) runs a per-ecosystem
detector. The npm detector (`find_dead_npm_modules`,
`rlsbl/dep_validation.py`) is BFS-from-entry-points: it resolves entry points
from package.json (`exports`, `main`, `bin`) via `_resolve_npm_entry_points`,
builds a file-level import graph over the project's source extensions, walks
from the entry points, and reports every production source file the walk never
reaches.

That model is exactly right for a plain-JavaScript package, where package.json
names the source files that ship.

## Problem

A TypeScript package does not point at its sources. Its package.json names
build output -- `"main": "dist/index.js"`, `"exports": {".":
"./dist/index.js"}`, `"bin": {"tool": "dist/cli.js"}` -- and the sources live
under `src/*.ts`. Two failures follow:

1. **Every `src/*.ts` is reported dead.** The BFS starts at `dist/index.js`
   (when a build exists) or at nothing (when it does not), and the import graph
   built from `dist/` reaches only other `dist/` files. `src/index.ts` is
   reachable from the published entry point *through the build step*, which the
   detector has no notion of. `_resolve_npm_file` maps a `.js` request to a
   `.ts` sibling, but that only helps when the two live in the same directory;
   it cannot express `dist/index.js` -> `src/index.ts`.
2. **Build output is walked as production source.** `dist/` is not excluded, so
   generated `.js` (and `.d.ts`) files are collected into the production set and
   can themselves be reported dead -- noise about files no human wrote, on top
   of the noise about the files they did.

The check is `warn` severity, so it does not block a release, but a warning that
names every source file in the package is a warning nobody reads -- and it
trains operators to add blanket `dead-modules.toml` exclusions, which then hide
genuinely dead modules (the very laundering the exclusion mechanism is careful
about elsewhere).

## Options

**Option A -- map declared output paths back to sources via tsconfig.**
Read `tsconfig.json`'s `compilerOptions.outDir` and `rootDir`; when an entry
point resolves inside `outDir`, rewrite it to the corresponding path under
`rootDir` with a source extension, and seed the BFS there. Add `outDir` to the
excluded directories for the production-file walk.

- Pros: fixes both failures from one declared fact; matches how the project
  itself defines the relationship; no new configuration.
- Cons: needs tsconfig parsing (JSONC -- comments and trailing commas are legal
  there, so `json.load` is not enough); multi-config setups (`tsconfig.build.json`)
  and path aliases are not covered.

**Option B -- treat every `src/**` file as an entry point for TS projects.**

- Pros: trivial.
- Cons: defeats the check entirely for TypeScript -- with every file an entry
  point, nothing can ever be dead. A check that cannot fail is worse than one
  that fails wrongly, because it looks like coverage.

**Option C -- skip the npm detector when package.json points into a build dir.**
Detect that every resolved entry point lies under a directory containing
generated output and skip with a stated reason.

- Pros: honest -- the detector says it cannot answer instead of answering
  wrongly; small; no parsing of a second config format.
- Cons: no dead-module detection at all for TypeScript packages, which is a
  large share of the fleet's npm projects.

**Option D -- exclude build output only (fix 2, not fix 1).**

- Pros: removes half the noise for a few lines.
- Cons: leaves every source file still reported dead. Not worth shipping alone.

Recommendation: A, with C as the fallback for the shapes A cannot resolve (no
tsconfig, or entry points that resolve nowhere) -- so the detector either
answers correctly or declines with a reason, and never answers wrongly.

## Affected files

- `rlsbl/dep_validation.py` -- `_resolve_npm_entry_points`, `_resolve_npm_file`,
  `_build_npm_import_graph`, `find_dead_npm_modules`, `walk_source_files`,
  `_is_non_production_path`
- `rlsbl/checks/quality.py` -- `check_dead_modules` (the npm branch and the
  reason strings)
- Tests: `tests/test_dep_validation.py`, `tests/test_dead_modules*.py` -- needs
  a TypeScript fixture whose package.json points at `dist/` with sources in
  `src/`, both with and without a build present

## Effort

Medium: JSONC-tolerant tsconfig reading plus the outDir/rootDir remap, and
fixtures for built, unbuilt, and tsconfig-less TypeScript packages.
