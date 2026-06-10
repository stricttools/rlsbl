# rlsbl uses system pytest instead of venv pytest in uv workspaces

## Problem

When rlsbl runs `uv sync` + `uv run pytest` from a uv workspace member directory (e.g., `shopkeep/core/`), pytest runs under system Python 3.14 instead of the project's pinned Python 3.13. This causes `ModuleNotFoundError: No module named 'asyncpg'` because asyncpg isn't installed in system Python.

## Root cause

`uv sync` (without flags) does an **exact sync** — it removes packages not in the current project's dependency scope. When run from a workspace member, it strips packages belonging to the workspace root's dev group and other members, including pytest.

`uv run pytest` then can't find pytest in the venv and falls through to the system PATH, finding `/home/m/.local/bin/pytest` (shebang `#!/usr/bin/python3`, system Python 3.14). That system pytest has no access to venv packages like asyncpg.

The sequence:

1. `testing.py:56` — `subprocess.run(["uv", "sync", "--quiet"], cwd=project_dir)`
   - Exact sync from member dir removes 10+ packages including pytest, pytest-asyncio, ruff
2. `testing.py:60` — `subprocess.run(["uv", "run", "pytest"], cwd=project_dir)`
   - pytest not in venv, falls through to `/home/m/.local/bin/pytest` (system Python 3.14)
   - System Python has no asyncpg — `ModuleNotFoundError`

Key behavioral difference:
- `uv sync` = exact (removes extraneous packages)
- `uv run` = inexact (additive only, never removes)

The explicit `uv sync` call actively destroys the venv state that `uv run` would have preserved.

## Investigated and rejected fixes

### A: `uv sync --all-groups`

Not viable. `--all-groups` only affects the **current project's** dependency groups. Workspace members typically have no groups (pytest is in the root's `dev` group). From a member dir, `--all-groups` is a no-op.

Also has side effects on standalone projects: pulls in all groups including heavy optional ones (e.g., PixelWeaver's `gui` group with PyGObject/pycairo).

### B: `uv run --all-groups pytest` (drop uv sync)

Not viable as a complete fix. Works by accident on existing venvs (inexact sync preserves pytest if already present), but on a fresh venv, `--all-groups` from a member still doesn't install the root's dev group. Pytest would not be installed.

Also loses the separate sync error handling (lines 57-59).

### C: Configurable `test_groups` in `.rlsbl/config.json`

Dead on arrival. `uv sync --group dev` from a workspace member **errors**: `Group 'dev' is not defined in the project's dependency-groups table`. The `--group` flag is scoped to the current project, not the workspace root.

Would also require a new config key on 84 pypi projects.

### D: Auto-detect which groups contain pytest

Same `--group` scoping problem as C. Even if we could detect the group name, we can't pass it from a member directory. Also adds fragile TOML parsing complexity.

### E: `uv sync --inexact`

Partial fix. Prevents pytest from being removed when already present, but on a **fresh venv**, it does NOT install pytest — the member's deps don't include it. Same bug on first clone.

## Chosen fix

### `uv sync --all-packages` + rlsbl check for unbuildable members

**The fix has two parts:**

### Part 1: Change `uv sync` to `uv sync --all-packages` in testing.py

`--all-packages` syncs all workspace members + root into the shared venv, including the root's dev dependency groups (where pytest lives).

Properties:
- Fixes the bug fully, including fresh venvs
- Safe no-op on standalone (non-workspace) projects — silent success, no errors
- Includes dev groups by default (no `--all-groups` needed)
- Works with `--quiet`
- Negligible performance impact (shopkeep: 17ms, large workspaces: ~300ms)
- No env var or config equivalent — must be a CLI flag

The change is in `rlsbl/testing.py:53`: add `"--all-packages"` to `sync_cmd`.

Also applies to all other `uv sync` call sites that run from workspace member directories:
- `rlsbl/testing.py:53` (test runner)
- `rlsbl/commands/release.py:434-435` (`uv run <entry_point> --dump-schema`)
- `rlsbl/commands/release.py:485-491` (`uv lock`)
- `rlsbl/commands/dev.py:76` (`uv sync` / `uv tool install`)

### Part 2: Add rlsbl check for unbuildable workspace members

`--all-packages` has one fragility: if ANY workspace member has a broken build config, the entire sync fails. This is a real scenario — the WWW workspace has a broken `www-scaleway-tem` member (missing `[tool.hatch.build.targets.wheel] packages` directive).

Add a new rlsbl check (likely under the `workspace` tag) that validates all uv workspace members can build wheels. This catches broken configs early via `rlsbl check`, not at release time.

Detection approach: `uv build --package <name> --wheel` per member. The failure is build-time only — `uv lock` and `uv sync --dry-run` both succeed with broken members.

Note: `uv sync --all-packages --no-install-package <broken>` exists as an escape hatch, but the chosen approach is to hard-error and require broken members to be fixed rather than silently excluding them.

## Affected files

- `rlsbl/testing.py:53` — main fix (add `--all-packages` to `uv sync`)
- `rlsbl/testing.py:60` — consider whether `uv run` also needs `--all-packages`
- `rlsbl/checks.py` — new check for unbuildable workspace members
- Other `uv sync`/`uv run` call sites in `commands/release.py`, `commands/dev.py`, `targets/pypi.py`, `pipelines/pypi.py`, `pipelines/build.py`

## Environment

- System Python: 3.14.5 (`/usr/bin/python3`)
- uv-managed Python: 3.13.11 (in `.venv/`)
- uv version: 0.9.17
- Fedora 43
- rlsbl shebang: `#!/usr/bin/python3` (system Python)

## Reproduction

```bash
cd ~/Projects/shopkeep/core
rlsbl release run --watch --yes
# Tests fail: pytest runs under Python 3.14.5, ModuleNotFoundError: asyncpg
```

## Verification

```bash
# Confirm uv sync --all-packages preserves pytest from member dir
cd ~/Projects/shopkeep/core
uv sync --all-packages --dry-run
# Should show "Would make no changes" (not "Would uninstall 10 packages")

# Confirm standalone projects are unaffected
cd ~/Projects/rlsbl
uv sync --all-packages --dry-run
# Should show "Would make no changes"
```
