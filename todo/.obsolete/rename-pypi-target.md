# Rename/split the `pypi` target

## Context

The `pypi` target currently means two things: "build wheels" and "publish to pypi.org". When combined with `private: true`, the publish step is skipped and wheels are attached to GitHub Releases instead. This works, but the naming is confusing — a private repo with `targets: ["pypi"]` reads as "we publish to PyPI" when the actual intent is "we build wheels."

## Problem

The target name conflates the build artifact format (wheel/sdist per PEP 427/PEP 625) with the distribution channel (pypi.org). Users of private repos have to know that `pypi + private` means "wheels attached to GitHub Releases" — this is non-obvious.

## Proposed solutions

1. **Rename `pypi` to `wheel`** (or `pep427`). Add `pypi` as an alias for backwards compatibility. The name then describes what's built, not where it goes. The `private` flag controls distribution.

2. **Split into `wheel` (build) + `pypi` (publish channel)**. Targets become composable: `targets: ["wheel"]` builds wheels, `targets: ["wheel", "pypi"]` builds and publishes. Private repos use just `["wheel"]`.

3. **Add a `channel` field** separate from `targets`. Targets describe artifacts, channels describe destinations. E.g., `targets: ["pypi"], channels: ["github-releases"]` vs `channels: ["pypi.org"]`.

## Affected files

- `rlsbl/targets/__init__.py` (TARGETS dict)
- `rlsbl/targets/pypi.py` (class name, detection logic)
- `rlsbl/commands/init_cmd.py` (scaffold template selection)
- All existing `config.json` files in consumer repos (migration needed)

## Effort

Medium — the rename itself is straightforward but requires a migration path for existing repos. Option 1 (alias) is lowest effort. Option 2/3 are more correct but higher effort.
