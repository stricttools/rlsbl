# Monorepo release creates stray per-package artifacts for releasable members

When releasing a releasable group in a monorepo, the release flow picks the first member package as the "representative" and writes per-package artifacts to that package's directory. These artifacts should go at the releasable level instead.

## Problem

Given a monorepo with a releasable group containing multiple packages (e.g., `veliu-dev` with 26 member packages), running `rlsbl monorepo release run` creates:

- `core/.rlsbl/version` (rlsbl tool version marker)
- `core/CHANGELOG.md` (per-package changelog)
- `core/.rlsbl/releases/` (release state directory, cleaned up after release but parent `.rlsbl/` persists)

Where `core` is the first member in `workspace.toml`. These files are committed by the release flow and pollute the package directory.

## Root causes

Three separate code paths in `rlsbl/commands/release/execute.py` and `release_state.py`:

1. **Release state** (`release_state.py:25`): `save_release_state()` calls `os.makedirs(core/.rlsbl/releases/, exist_ok=True)`, creating `core/.rlsbl/` as a side effect. `clear_release_state()` removes the state file and `releases/` dir but not the parent `core/.rlsbl/`.

2. **Version marker** (`execute.py:852-863`): Writes `core/.rlsbl/version` because `core/.rlsbl/` now exists from step 1.

3. **CHANGELOG.md** (`execute.py:868-873`): Generates `core/CHANGELOG.md` at the per-package level. For releasable members, this should be skipped or placed at the releasable level.

## Expected behavior

For releasable releases:
- Release state should go to `.rlsbl-monorepo/releasables/{name}/releases/`
- Version marker should be at the releasable level, not per-package
- Per-package CHANGELOG.md should be skipped (the releasable-level CHANGELOG is already generated correctly)

## Additional bug found

`rlsbl changelog add` ignores `--dry-run` (`changelog_cmd.py:347`): `cmd_add` calls `append_entry()` unconditionally without checking the dry-run flag.

## Workaround

Gitignore the stray files:
```
core/.rlsbl/changes/
core/.rlsbl/config.json
```

Allow `core/.rlsbl/version` through since the release flow commits it.
