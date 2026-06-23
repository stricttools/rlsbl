# PypiScanner false positive when PyPI names differ from workspace names

## Problem

`rlsbl check --tag workspace` (`deps-undeclared`) reports false positives when a workspace project's PyPI package name differs from its workspace project name.

Example: a workspace has project `transport` (path `transport/`) whose `pyproject.toml` declares `[project] name = "orxtra-transport"`. Another project `services` depends on `"orxtra-transport"` in its `pyproject.toml`.

`deps-undeclared` correctly detects that `services` imports `orxtra.transport` (via the import scanner's namespace_map). But it does NOT recognize that the declared dependency `orxtra-transport` satisfies the import, because `PypiScanner.scan()` builds a lookup from workspace project names (`transport`) but the dependency is declared using the PyPI name (`orxtra-transport`).

## Root cause

`PypiScanner.scan()` builds `{normalize_pypi(name): name for name in workspace_names}` where workspace names are bare directory names (`transport`, `services`, etc.). When it encounters a dependency like `orxtra-transport` in a pyproject.toml, `normalize_pypi("orxtra-transport")` does not match `normalize_pypi("transport")`.

The import scanner works correctly because it uses `namespace_map` with sub-component matching. But the dependency declaration scanner has no equivalent reverse mapping.

## Fix

Build a reverse mapping from each project's actual `[project].name` in pyproject.toml to its workspace project name. In `PypiScanner.scan()`, check both the workspace name and the actual PyPI name when matching dependencies.

## Impact

Affects any monorepo where PyPI package names have a prefix/suffix (e.g., `myorg-core`, `myorg-utils`) while workspace project names are bare (`core`, `utils`). All intra-workspace dependencies declared via pyproject.toml will be reported as undeclared.
