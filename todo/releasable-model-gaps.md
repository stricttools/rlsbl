# Releasable model gaps

The releasable model (v0.78.0) has four gaps discovered during first-release preparation of a monorepo with 16 sub-projects grouped under one releasable.

## 1. Publish workflow ignores releasable tags

`rlsbl monorepo sync` generates publish.yml with per-project tag conditions (`startsWith(tag, 'protocols@v')`). In explicit releasable mode, the release flow creates a single tag per releasable (e.g., `orxt@v0.1.0`). None of the per-project conditions match, so zero publish jobs fire.

- Root cause: `_get_monorepo_tag_prefix` in `commands/monorepo/sync.py` (line ~197-211) always uses `project["name"]`. `generate_inline_publish_router` in `commands/monorepo/publish_inline.py` (line ~204-243) has zero releasable awareness.
- Fix: when a project has `releasable = "X"`, the publish job's tag prefix should be `X@v` (from the releasable's `tag_format`), not `project_name@v`.
- Impact: blocks any monorepo using the releasable model from publishing via CI.

## 2. UV_NO_SOURCES hardcoded in CI template

`templates/pypi/ci.yml.tpl` (line 11) hardcodes `UV_NO_SOURCES: "1"`. This was added in v0.68.2 to fix CI for standalone projects with `path = "..."` sources that don't exist on runners. But for monorepo members using `workspace = true` sources, it breaks CI — workspace packages aren't on PyPI yet (first release) or should be tested from source (ongoing development).

- Root cause: the template has no conditional for monorepo vs standalone context.
- Fix: omit `UV_NO_SOURCES` for monorepo member projects (detectable during scaffold). Keep it for standalone projects with path sources. Ideally, wait for upstream uv `--no-path-sources` flag (uv issue #19701) which would ignore `path` sources while preserving `workspace = true`.
- Impact: blocks CI for any monorepo doing a first release, and causes ongoing CI to test against stale PyPI versions instead of workspace source.

## 3. Namespace package false positives in deps-unused

The `deps-unused` check (via `PythonAstLinter.scan_imports`) extracts the top-level module from imports. For namespace packages (e.g., `from orxt.protocols._tool import Tool`), it extracts `orxt` and checks against workspace names `{"protocols", "secrets", ...}`. No match, so every workspace import is invisible. In a 16-project monorepo, this produces 25+ false positives.

- Root cause: `lint/python_ast.py` (line ~114) extracts only the first segment. `import_scanners.py` (line ~140) normalizes and compares against workspace names.
- Fix: for namespace packages, check the second path segment against workspace names. The workspace root's `pyproject.toml` `[tool.uv.workspace].members` or the `src/` directory structure can identify the namespace.
- Impact: `rlsbl check --all` always fails. Not release-blocking (untagged check), but noisy and undermines trust in the check suite.

## 4. No CLI command for releasable migration

The migration from implicit to explicit releasable mode requires calling Python functions directly (`consolidate_changelogs()`, `cleanup_per_package_release_state()`). There is no CLI command like `rlsbl monorepo migrate-releasable`.

- The functions exist in `releasable_migration.py` and `releasable_cleanup.py` but aren't wired to any CLI entry point.
- A CLI command should: read workspace.toml, consolidate per-package changelogs into the releasable-level changelog (with `packages` attribution), clean up per-package changelog directories (via saferm), and report what it did.
- Impact: usability gap. Monorepos migrating to the releasable model need to write ad-hoc Python to run the migration.

## Affected files

- `commands/monorepo/sync.py` — `_get_monorepo_tag_prefix`
- `commands/monorepo/publish_inline.py` — `generate_inline_publish_router`
- `templates/pypi/ci.yml.tpl` — hardcoded `UV_NO_SOURCES`
- `lint/python_ast.py` — top-level module extraction
- `import_scanners.py` — workspace name matching
- `releasable_migration.py` — no CLI entry point
- `releasable_cleanup.py` — no CLI entry point
