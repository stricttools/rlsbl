# Clean up checks bootstrap: use public API instead of private internals

## Context

strictcli is removing CWD-based auto-discovery of `.strictcli/checks.toml`. The only way to enable the check system is now `checks_path=` (Python) / `WithChecks(path)` (Go). This obsoletes rlsbl's current bootstrap hack.

## Current state

`rlsbl/__init__.py` lines 172-187: `_bootstrap_checks()` uses four private strictcli internals:

- `app._check_defs = defs` (direct attribute write)
- `app._checks_enabled = True` (direct attribute write)
- `app._register_check_command()` (private method call)
- `from strictcli import _load_checks_toml` (private function import)

This exists because rlsbl ships its checks.toml inside the package (`rlsbl/data/checks.toml`) rather than relying on CWD discovery — rlsbl runs from arbitrary user project directories.

## What to do

1. **Use `checks_path=` on the `App()` constructor.** Pass `checks_path=Path(__file__).parent / "data" / "checks.toml"` to `strictcli.App(...)`. This replaces lines 174-182 of the bootstrap — strictcli handles loading, enabling, and registering the check command.

2. **Simplify bootstrap to just `register_checks(app)`.** The only remaining work after `App()` construction is registering the 27 check implementations. The `_bootstrap_checks()` function can be replaced with a single `register_checks(app)` call at module level.

3. **Add `app = "rlsbl"` to `rlsbl/data/checks.toml`.** Required by strictcli's new schema — the `app` field must be present and must match the App's name.

4. **Delete the vestigial `.strictcli/checks.toml`** in the rlsbl repo root. This empty file (just `[checks]`) was a workaround for CWD-based discovery conflicts. With CWD discovery removed, it serves no purpose.

## Effort

Small — straightforward mechanical cleanup once the strictcli changes land.
