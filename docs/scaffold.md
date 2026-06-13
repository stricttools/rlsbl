---
description: "How rlsbl scaffold generates CI workflows, git hooks, and config files, with three-way merge preserving your customizations on repeated updates."
---

# Scaffold system

`rlsbl scaffold` generates and updates CI workflows, git hooks, changelog infrastructure, and configuration files for your project. It is safe to run repeatedly -- on re-run, it performs a three-way merge to preserve your customizations while applying template updates.

## What scaffold creates

| File / directory | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | CI workflow (tests on push/PR) |
| `.github/workflows/publish.yml` | Publish workflow (triggered on GitHub Release) |
| `.git/hooks/pre-push` | Pre-push hook calling `rlsbl check --tag prepush` |
| `.rlsbl/config.json` | Project configuration (targets, pipelines, private flag) |
| `.rlsbl/changes/unreleased.jsonl` | JSONL changelog for unreleased commits |
| `.rlsbl/hooks/pre-checks.sh` | User-owned pre-checks hook (runs before tests) |
| `.rlsbl/hooks/pre-release.sh` | Scaffold-managed pre-release hook |
| `.rlsbl/hooks/post-release.sh` | Scaffold-managed post-release hook |
| `.rlsbl/bases/` | Merge bases for three-way merge (internal) |
| `.rlsbl/hashes.json` | File hashes for change detection (internal) |
| `.rlsbl/version` | Records which rlsbl version generated the scaffolding |
| `.gitignore` | Additions for build artifacts and rlsbl internals |
| `CHANGELOG.md` | Generated changelog (created once, never overwritten) |

## Three-way merge

When scaffold runs on a project that already has scaffolded files, it performs a three-way merge to reconcile template updates with your local modifications. This ensures that upgrading rlsbl never silently overwrites your customizations to CI workflows, hooks, or configuration files, while still applying any template improvements from newer versions.

### How it works

After each scaffold run, the rendered template content is saved as a **base** in `.rlsbl/bases/<target-path>`. This base acts as the common ancestor for the next merge. On the next scaffold run, three versions exist for each file, allowing scaffold to compute a precise diff between what changed on each side:

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
| `{{go.minRequiredGo}}` | Go target | `1.21` |

### Required variables

Certain variables (`name`, `registryUrl`) are mandatory for every target and must be provided by the target's `template_vars()` method. If a required variable is missing, scaffold raises a `ValueError` at render time rather than leaving unresolved `{{...}}` placeholders in the output, which would cause CI workflow failures that are harder to diagnose.

### Escaped placeholders

Templates that need literal `{{...}}` in their output (e.g., Docker metadata-action's `{{version}}`) use the escape syntax `\{{...}}`. The backslash is consumed during rendering, and the braces pass through unchanged. Without escaping, scaffold would attempt to resolve them as rlsbl variables and raise an error for unrecognized names.

### Action placeholders

`{{action "owner/name"}}` placeholders resolve against rlsbl's central action-version table (`rlsbl/data/action_versions.toml`), pinning GitHub Actions to known-good versions across all scaffolded projects. An unknown action name is a hard error, ensuring that every action reference in generated workflows maps to a verified version.

## Pre-push hook

Scaffold installs `.git/hooks/pre-push` with the V5 hook template, which captures git's stdin into `RLSBL_PUSH_STDIN` and runs `rlsbl check --tag prepush`. This hook enforces changelog coverage and other pre-push validations on every push, blocking pushes that would bypass the JSONL commit coverage requirement.

### Safe upgrade via hash detection

The hook content has changed across 5 rlsbl versions (V1 through V5), each introducing new checks or changing the invocation pattern. To safely upgrade hooks without clobbering user customizations or losing any manual additions, scaffold uses SHA-256 fingerprinting against a known set of historical hook hashes:

1. Reads the existing hook file
2. Computes its SHA-256 hash (trailing whitespace stripped for tolerance)
3. Compares against the set of all known historical hook hashes (V1 through V5)
4. If it matches a known hash (e.g., V4 or earlier): overwrite with the current V5 version
5. If it does not match: leave untouched and print a diff warning

This means any hook content you write yourself (or modify from the scaffold version) is permanently safe from scaffold overwrites. Projects with V4 hooks (which called `rlsbl pre-push-check`) are safely upgraded to V5 (which calls `rlsbl check --tag prepush`) on the next `rlsbl scaffold` run.

## Monorepo scaffold

In a monorepo workspace, each sub-project is scaffolded independently in its own directory. Afterward, `rlsbl monorepo sync` copies the generated workflow files from each project into the shared `.github/workflows/` directory at the repository root.

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
