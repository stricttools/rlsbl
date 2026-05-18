# Doc lint failures blocking releases

## Context

The v0.32.0 release had to be cut with `--skip-docs` because `selfdoc check` (run as part of the built-in release flow) reports multiple errors. The errors are pre-existing doc hygiene issues plus undocumented new modules added in v0.32.0.

Once this todo is addressed, `rlsbl release` should no longer require `--skip-docs`.

## Errors observed

1. **SEO006 — Missing `description:` frontmatter**
   - `docs/ci-customization.md` (new in v0.32.0)
   - Possibly others — re-run `selfdoc check` to confirm

2. **STALE001 — Stale frontmatter**
   - `docs/rlsbl-commands-init_cmd.md`
   - `docs/rlsbl.md`
   - These docs reference content that has drifted from the source. The init_cmd doc is especially likely to be stale after Phase 3's two-pass refactor.

3. **Undocumented symbols in new modules** (added in v0.32.0)
   - `rlsbl/action_versions.py` (Phase 2)
   - `rlsbl/commands/dev.py` (Phase 7)
   - `rlsbl/hook_hashes.py` (Phase 3)

## Fix

1. Add `description:` frontmatter to `docs/ci-customization.md`
2. Regenerate or refresh the stale docs (`selfdoc build` should pick up the changes, or use whatever the selfdoc refresh command is)
3. Add module-level docstrings + per-function docstrings to:
   - `rlsbl/action_versions.py` — `get_action_version`, `format_action`, `get_all_versions`, `UnknownActionError`
   - `rlsbl/commands/dev.py` — `run_install`, `_install_single`, `_install_monorepo`
   - `rlsbl/hook_hashes.py` — `compute_hook_hash`, the module-level constants

## Verification

After fixing, run `selfdoc check` from the project root and confirm exit 0. Then a normal `rlsbl release patch --yes` should succeed without `--skip-docs`.

## Affected files

- `docs/ci-customization.md`
- `docs/rlsbl-commands-init_cmd.md`
- `docs/rlsbl.md`
- `rlsbl/action_versions.py`
- `rlsbl/commands/dev.py`
- `rlsbl/hook_hashes.py`

## Effort

Small. Mostly adding docstrings and one frontmatter block. The stale docs may need investigation to figure out what regeneration step they need.
