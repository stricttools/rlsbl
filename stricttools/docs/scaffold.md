+++
description = "How rlsbl scaffold generates CI workflows, git hooks, the scratch directories and each ecosystem's private-path exclusions, the settings that keep a project's test runner out of its nested members, and why the workspace root is never scaffolded."
+++

# Scaffold system

`rlsbl scaffold` generates and updates CI workflows, git hooks, changelog infrastructure, and configuration files for your project. It is safe to run repeatedly -- on re-run, it performs a three-way merge to preserve your customizations while applying template updates.

## What scaffold creates

| File / directory | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | CI workflow (tests on push/PR) |
| `.github/workflows/publish.yml` | Publish workflow (triggered on GitHub Release) |
| `.git/hooks/pre-push` | Pre-push hook calling `rlsbl failing-checks --hook pre-push` |
| `.rlsbl/config.json` | Project configuration (targets, pipelines, private flag) |
| `.rlsbl/changes/unreleased.jsonl` | JSONL changelog for unreleased commits |
| `.rlsbl/hooks/pre-checks.sh` | User-owned pre-checks hook (runs before tests) |
| `.rlsbl/hooks/pre-release.sh` | Scaffold-managed pre-release hook |
| `.rlsbl/hooks/post-release.sh` | Scaffold-managed post-release hook |
| `.rlsbl/bases/` | Merge bases for three-way merge (internal); a releasable member's live in its releasable's state directory instead (see below) |
| `.rlsbl/hashes.json` | File hashes for change detection (internal) |
| `.rlsbl/version` | Records which rlsbl version generated the scaffolding; not written for a releasable member |
| `.gitignore` | Additions for build artifacts and rlsbl internals |
| `CHANGELOG.md` | Generated changelog (created once, never overwritten) |
| `experiments/.gitignore` | Makes `experiments/` a scratch directory git carries but never fills |
| `screenshots/.gitignore` | Makes `screenshots/` a scratch directory git carries but never fills |
| `experiments/go.mod`, `screenshots/go.mod` | Go projects only: keeps the go command out of the scratch directories |
| `.rlsbl/go.mod`, and a `go.mod` in each other private directory at the root that git tracks files in | Go projects only: keeps the private directory out of the module zip (see [Private paths](#private-paths)) |
| `.npmignore` | npm projects: created once, carrying the private-path entries |

## Scratch directories

Scaffold creates two scratch directories at the project root, `experiments/` and `screenshots/`, so that a tool or an agent that needs somewhere to put a throwaway file has a declared place for it inside the project instead of reaching for a system temporary directory.

- `experiments/` holds throwaway probes: prototypes, produced repositories, captures, one-off outputs -- anything written to find something out rather than to ship.
- `screenshots/` holds mid-work screenshots taken while verifying a piece of work. It is not a home for published images; those belong in a committed directory of their own.

Each directory carries a committed `.gitignore` whose whole content is:

```gitignore
*
!.gitignore
```

That spelling is what makes the directory exist in a fresh clone, so tooling may rely on it being there, while nothing inside it can be committed by accident. A line in the repository's root `.gitignore` would leave the directory absent after a clone, which is the opposite of the point.

The ignore file un-ignores exactly the files scaffold itself commits into the directory. In a Go project that is one file more, so the same file reads:

```gitignore
*
!.gitignore
!go.mod
```

Re-running scaffold over a project whose scratch directories already hold work changes nothing: the files scaffold writes into them are scaffold-managed like any other, so the merge is a no-op and the directory's contents are never touched.

A scratch directory that already holds files git TRACKS (other than the ones scaffold commits there) is refused before scaffold writes anything: under an ignore-everything rule those files would stay tracked while every new file beside them is silently ignored. The refusal lists the files and the ways out: move them to a committed home (images a reader sees belong in `assets/`, e.g. `mkdir -p assets && git mv screenshots/hero.png assets/hero.png`), or, when the directory is not scratch space at all, rename it; commit the move and re-run scaffold.

### The project's own test runner skips them too

Pruning the directories from rlsbl's walks (below) keeps rlsbl's checks green and says nothing to the project's test runner. A `test_*.py` left in `experiments/` is still collected by a bare `pytest`, and a `.go` file there without its own module file is still built by `go test ./...` -- so a half-finished probe breaks a suite it has nothing to do with, which is the opposite of what a disposable scratch directory is for.

There is no cross-ecosystem setting for this, so scaffold writes the one each ecosystem's runner honors. Which mechanism a target uses is declared on the target itself and appears in the [support matrix](targets.md) as `scratch_test_exclusion`:

| Ecosystem | What scaffold writes | Where |
| --- | --- | --- |
| Python | Both directory names added to `norecursedirs`, alongside pytest's own default patterns | `[tool.pytest.ini_options]` in `pyproject.toml` |
| Go | A `go.mod` in each scratch directory, and the ignore exception that carries it into a clone | `experiments/go.mod`, `screenshots/go.mod` |
| npm | Nothing | -- |

Three points about that table:

- **pytest's `norecursedirs` replaces its default rather than adding to it**, so scaffold writes pytest's default patterns back alongside the two scratch names. Without that, a scaffolded project would start collecting from `build/`, `dist/` and `node_modules/`. A project that already has the option keeps its own entries, with only the missing scratch names appended.
- **The Go route is a nested module, not a renamed directory.** `go test ./...` skips a directory that declares its own module, and also one whose name begins with `_` or `.`; the scratch directories keep their plain names, which the convention, every walk and every document here spell out, so the module file is what is left. It costs one more committed file per scratch directory, plus the `!go.mod` line in that directory's ignore file that lets it reach a fresh clone, and it makes a probe placed there a separate module: it cannot import the parent module without a `replace` directive of its own. Without the committed marker, a fresh clone plus one dropped `.go` file is a broken `go test ./...`, which is the case the whole mechanism exists for.
- **npm is left out on purpose.** `npm test` runs the project's own test script, which names whichever runner the project chose -- jest, vitest, mocha, `node --test` -- each with its own configuration file, several of them executable JavaScript. rlsbl writes none of those files, and it will not start owning one to place a single setting, so it writes nothing and says so here. The same holds for any ecosystem whose entry reads `no-test-runner-recursion`: its runner collects only from a declared test source set, so a scratch directory is never reached in the first place.

**Nested members are skipped the same way, one path each.** A member whose directory encloses other members (see [Nested members](monorepo.md#nested-members)) gets a path-exact exclusion per nested member, so its runner never collects their tests: `--ignore=<path>` in pytest's `[tool.pytest.ini_options] addopts`. A basename in `norecursedirs` would also skip an unrelated directory of the same name, which is why these are paths. pytest resolves `--ignore` against the directory it starts in, which is the member's own in rlsbl's runs and in CI; run a member's tests from there. The `nested-member-runner-exclusion` check fails while an exclusion is missing, including where scaffold could not write it. Go needs nothing (a nested Go module has its own `go.mod`), and npm is the same stated gap as above.

`pyproject.toml` belongs to the project, not to scaffold. Scaffold merges the one setting into it and byte-preserves everything else, including comments and key order; a second run is a no-op. It never enters the managed-files registry, so the orphan sweep can never delete it. Where the setting cannot be placed safely -- a `pytest.ini`, which outranks `pyproject.toml` as pytest's configuration file -- scaffold writes nothing and prints the exact line to add by hand.

### Checks never look inside them

rlsbl's repository walks -- dead-module detection, circular-dependency detection, the library linters, the dependency import scans, the ldflags Go source reader, and the unregistered-project scan -- read the filesystem rather than git's index, so a `.gitignore` alone would not keep them out. Every one of those walks prunes both directory names at the root of the project it is walking, which is what lets a throwaway git repository or a stray source file inside `experiments/` leave every check green.

The pruning is scoped to the project root, matching where scaffold creates them. A directory named `experiments` or `screenshots` nested deeper inside a project is an ordinary source directory and is walked normally. In a workspace, every member is walked with its own directory as the root, so each member's own scratch directories are pruned.

## Private paths

A registry keeps every upload permanently, so a release refuses an upload that carries a private path: planning notes, release and family metadata, agent instructions, scratch output, or environment files (the list is in the `upload-private-paths` row of [the checks reference](checks.md#project-checks)). Scaffold writes each ecosystem's own exclusion, so a new project passes that refusal without hand edits:

| Ecosystem | What scaffold writes | Where |
| --- | --- | --- |
| Python (hatchling) | The private-path entries, merged into `exclude` (existing entries kept); `uv build` builds the wheel from this sdist | `[tool.hatch.build.targets.sdist]` in `pyproject.toml` |
| npm | The private-path entries, in the file scaffold creates once and never touches again | `.npmignore` |
| Go | A stub `go.mod` in `.rlsbl/` and in each other private directory at the module root that git tracks files in; Go leaves a directory holding its own `go.mod` out of the module zip | `<directory>/go.mod` |

A Python project built with another backend gets a printed warning naming that backend's exclusion instead, since scaffold writes only hatchling's; its CI refuses the upload until the entries are added. `.npmignore` is user-owned, so a project scaffolded before these entries existed adds them by hand when the refusal names them.

Go cannot leave a single file out of a module zip, only a directory holding its own `go.mod`. A private file at the module root therefore moves or stops being committed: `CLAUDE.md` moves to `.claude/CLAUDE.md` (Claude Code reads it there) with a stub `go.mod` in `.claude/`, and an environment or local-only file leaves git. A `CLAUDE.md` that selfdoc generated (its first line is selfdoc's generated-file header) is not moved by hand, since selfdoc would keep generating it at the root: the refusal names upgrading selfdoc and running `selfdoc layout migrate`, which moves it to `.claude/CLAUDE.md` and commits, and then `rlsbl scaffold`, which writes the stub `go.mod` in `.claude/` once git tracks a file there.

## Three-way merge

When scaffold runs on a project that already has scaffolded files, it performs a three-way merge to reconcile template updates with your local modifications. This ensures that upgrading rlsbl never silently overwrites your customizations to CI workflows, hooks, or configuration files, while still applying any template improvements from newer versions.

### How it works

After each scaffold run, the rendered template content is saved as a **base** in `.rlsbl/bases/<target-path>`. In a monorepo, a member that belongs to a releasable keeps no release state of its own (the `releasable-residue` check refuses it), so its bases are saved where the workspace keeps that member's state: `.rlsbl-monorepo/releasables/<releasable>/bases/<member path>/<target-path>`; and no `.rlsbl/version` marker is written for it. A nested member of the same releasable therefore keeps its bases inside its enclosing member's (`bases/gfx/shader/` inside `bases/gfx/`), and scaffolding the enclosing member leaves them alone rather than sweeping them as orphans. Every base a run writes is committed with it, including one seeded for a file that already matched its template. This base acts as the common ancestor for the next merge. On the next scaffold run, three versions exist for each file, allowing scaffold to compute a precise diff between what changed on each side:

- **Ours**: the file currently on disk (may include your edits)
- **Base**: the last scaffolded version (what was written last time)
- **Theirs**: the new template output (what scaffold wants to write now)

### Merge decision table

| Condition | Action | Reason |
| --- | --- | --- |
| ours == theirs | Skip (no change) | File already matches the new template |
| ours == base | Take theirs | You did not customize; template updated |
| base == theirs | Keep ours | Template unchanged; your edits preserved |
| All three differ | Run `git merge-file` | Both sides changed; attempt automatic merge |

When `git merge-file` cannot resolve all hunks, conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) are left in the file. Conflicted files are excluded from the auto-commit so they show up in `git status` for manual resolution.

### No base stored

For legacy projects scaffolded before the three-way merge system existed, there is no base file stored in `.rlsbl/bases/`. Without a common ancestor, scaffold cannot determine which side changed, so it takes a conservative approach to avoid overwriting your work. In this case:

- If the file on disk matches the new template: seed the base and skip.
- If they differ: save the new template as base for next time but do not overwrite. A warning is printed advising `scaffold --force` to reset.

## File ownership

Scaffold distinguishes two ownership categories that determine update behavior. User-owned files are created once and never touched again by scaffold, even with `--force` — they are fully yours to modify. Scaffold-managed files are maintained via the three-way merge system described above, receiving template updates while preserving your local edits wherever possible.

### User-owned files

These files are created once by scaffold and **never overwritten or merged**, even with `--force`. They are fully yours to modify, delete, or extend. Scaffold will not touch them on subsequent runs, ensuring your changelog entries, custom CI jobs, and hook logic remain exactly as you wrote them:

- `CHANGELOG.md`
- `.npmignore`
- `.dockerignore`
- `.rlsbl/hooks/pre-checks.sh`
- `.rlsbl/changes/unreleased.jsonl`
- `.github/workflows/ci-custom.yml`
- `.github/workflows/publish-custom.yml`

### Scaffold-managed files

These files are created and maintained by scaffold via three-way merge. When a new rlsbl version updates their templates, scaffold merges the changes into your copy, preserving any local edits you have made while incorporating the template improvements:

- `.github/workflows/ci.yml`
- `.github/workflows/publish.yml`
- `.rlsbl/hooks/pre-release.sh`
- `.rlsbl/hooks/post-release.sh`
- `.gitignore`

## The --force flag

`--force` overwrites all scaffold-managed files with the current template output, ignoring stored bases and skipping the three-way merge entirely. After `--force`, new bases are saved for the freshly written content. This is a destructive operation that discards all local customizations to scaffold-managed files, so use it only when a clean reset is needed.

`--force` does **not** touch user-owned files. Those are always safe from overwrite regardless of flags.

Use `--force` when:

- Upgrading from a pre-merge-era scaffold (no bases stored)
- Resolving persistent merge conflicts by resetting to the latest template
- Recovering from a corrupted `.rlsbl/bases/` directory

## Template variables

Templates use `{{variableName}}` placeholders resolved at scaffold time. Variables come from the target's `template_vars()` method and project metadata. Each release target (npm, pypi, go, etc.) provides its own set of variables, and scaffold renders all templates in a single pass — unresolved placeholders are treated as hard errors rather than being left in the output as broken references.

Common variables:

| Variable | Source | Example |
| --- | --- | --- |
| `{{name}}` | Package/project name | `rlsbl` |
| `{{registryUrl}}` | Target registry URL | `https://pypi.org/project/rlsbl` |
| `{{pypi.minRequiredPython}}` | Python target | `3.11` |
| `{{npm.minRequiredNode}}` | npm target | `18` |
| `{{npm.nodeMatrix}}` | npm target: every supported Node line (20, 22, 24) whose newest release `engines.node` admits; the CI workflow's `node-version` matrix. A `package.json` with no `engines.node` is refused at scaffold, naming the declaration to add | `[22, 24]` |
| `{{go.minRequiredGo}}` | Go target | `1.21` |

### Required variables

Certain variables (`name`, `registryUrl`) are mandatory for every target and must be provided by the target's `template_vars()` method. If a required variable is missing, scaffold raises a `ValueError` at render time rather than leaving unresolved `{{...}}` placeholders in the output, which would cause CI workflow failures that are harder to diagnose.

### Escaped placeholders

Templates that need literal `{{...}}` in their output (e.g., Docker metadata-action's `{{version}}`) use the escape syntax `\{{...}}`. The backslash is consumed during rendering, and the braces pass through unchanged. Without escaping, scaffold would attempt to resolve them as rlsbl variables and raise an error for unrecognized names.

### Action placeholders

`{{action "owner/name"}}` placeholders resolve against rlsbl's central action-version table (`rlsbl/data/action_versions.toml`), pinning GitHub Actions to known-good versions across all scaffolded projects. An unknown action name is a hard error, ensuring that every action reference in generated workflows maps to a verified version.

## Pre-push hook

Scaffold installs `.git/hooks/pre-push` with the current hook template, which captures git's stdin into `RLSBL_PUSH_STDIN` and runs `rlsbl failing-checks --hook pre-push`. The hook names only the hook: `[hooks.pre-push]` in rlsbl's `checks.toml` declares which checks it runs. It enforces changelog coverage and other pre-push validations on every push, blocking on error-level failures only, so pushes that would bypass the JSONL commit coverage requirement are refused while a check an options entry softened to `warn` never blocks.

### Safe upgrade via hash detection

The hook content has changed across rlsbl versions, each introducing new checks or changing the invocation pattern. To safely upgrade hooks without clobbering user customizations or losing any manual additions, scaffold uses SHA-256 fingerprinting against a known set of historical hook hashes:

1. Reads the existing hook file
2. Computes its SHA-256 hash (trailing whitespace stripped for tolerance)
3. Compares against the set of every hook version rlsbl has shipped (`rlsbl/hook_hashes.py`)
4. If it matches a known hash: overwrite with the current version
5. If it does not match: leave untouched and print a diff warning

This means any hook content you write yourself (or modify from the scaffold version) is permanently safe from scaffold overwrites. Projects whose hook calls `rlsbl pre-push-check` or `rlsbl check --tag prepush` are safely upgraded to the current hook (which calls `rlsbl failing-checks --hook pre-push`) on the next `rlsbl scaffold` run.

## Monorepo scaffold

In a monorepo workspace, each sub-project is scaffolded independently in its own directory. Afterward, `rlsbl monorepo sync` copies the generated workflow files from each project into the shared `.github/workflows/` directory at the repository root.

The workspace root is not scaffolded. `rlsbl scaffold` run there does nothing and says so, whatever kind the root member is: its CI comes from `rlsbl monorepo sync`, which renders the router and publish workflows at the root, and every other file the root member needs is written by hand. A message about something missing at the root names that hand edit, not `rlsbl scaffold`: a root member whose target keeps its version in a file scaffold would create (`VERSION`, `version.json`) and has none is told to create it by hand holding the root member's current version (for a root member in a releasable, the version its releasable's version file records), and the root member's test-runner exclusions for nested members are named the same way (see [Nested members](monorepo.md#nested-members)).

```bash
# Scaffold a specific sub-project
cd packages/mylib
rlsbl scaffold

# Sync all workflows to the repo root
cd /repo-root
rlsbl monorepo sync
```

Each sub-project gets its own `.rlsbl/` directory with config, hooks, and changelog infrastructure. The monorepo root has `.rlsbl-monorepo/workspace.toml` that coordinates the workspace.

## Related checks

Two checks detect scaffold problems that would otherwise surface only at CI time or cause silent misbehavior. Both run as part of `rlsbl check --all`, so they are evaluated automatically during the release pipeline and can also be invoked independently for quick verification after a scaffold run.

| Check | Severity | What it detects |
| --- | --- | --- |
| `scaffold-unreplaced-vars` | error | Leftover `{{...}}` placeholders in workflow files that were not resolved during scaffold |
| `scaffold-conflicts` | error | Unresolved `<<<<<<< ` / `>>>>>>> ` conflict marker pairs from a three-way merge, in the managed-files registry, `.github/workflows/`, or anywhere under `.rlsbl/` |

Run them with:

```bash
rlsbl check --name scaffold-unreplaced-vars
rlsbl check --name scaffold-conflicts
```

`scaffold-unreplaced-vars` runs under `rlsbl check --tag quality`. `scaffold-conflicts` runs under the `project`, `prepush`, and `release` tags, and also runs as a pre-mutation guard at the start of `rlsbl release run`.

## Examples

### First-time scaffold for a Python project

```bash
cd ~/Projects/mylib

# Scaffold CI, hooks, and changelog infrastructure
rlsbl scaffold
#   Created .rlsbl/config.json
#   Created .rlsbl/changes/unreleased.jsonl
#   Created .rlsbl/hooks/pre-checks.sh
#   Created .rlsbl/hooks/pre-release.sh
#   Created .rlsbl/hooks/post-release.sh
#   Created .github/workflows/ci.yml
#   Created .github/workflows/publish.yml
#   Created .git/hooks/pre-push
#   Updated .gitignore
#   Created CHANGELOG.md

# Verify detected targets
rlsbl targets
#   Target   Detected   Version file
#   pypi     yes        pyproject.toml
```

### Re-running scaffold after upgrading rlsbl

```bash
# Template changed, your edits preserved via three-way merge
rlsbl scaffold
#   Merged .github/workflows/ci.yml (template updated, your edits preserved)
#   Merged .github/workflows/publish.yml (template updated, your edits preserved)
#   Skipped .rlsbl/hooks/pre-checks.sh (user-owned)
#   Skipped .rlsbl/changes/unreleased.jsonl (user-owned)
```

### Resolving merge conflicts after scaffold

When both the template and your customizations have changed the same lines since the last scaffold run, the three-way merge produces conflict markers in the output file. These are standard git-style conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) that must be resolved manually before the next release, because `rlsbl check --name scaffold-conflicts` detects unresolved markers and blocks the release preflight. To resolve:

```bash
rlsbl scaffold
#   CONFLICT in .github/workflows/ci.yml -- resolve manually

# Check for conflict markers
rlsbl check --name scaffold-conflicts
#   scaffold-conflicts ............ FAIL
#     .github/workflows/ci.yml: unresolved conflict markers

# Edit the file to resolve conflicts, then commit
```

### Resetting scaffold-managed files to template defaults

```bash
# Overwrite all scaffold-managed files (user-owned files are safe)
rlsbl scaffold --force
#   Wrote .github/workflows/ci.yml (forced)
#   Wrote .github/workflows/publish.yml (forced)
#   Skipped .rlsbl/hooks/pre-checks.sh (user-owned, never overwritten)
```
