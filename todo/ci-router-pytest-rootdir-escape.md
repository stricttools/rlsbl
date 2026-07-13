# Inlined CI router: member pytest runs escape to the workspace root

## Context

The inlined CI router (`rlsbl/commands/monorepo/sync.py::_generate_router`) emits
per-member jobs of the shape `uv sync && uv run pytest tests/` with
`working-directory: <member>`.

## Problem

pytest's rootdir discovery walks upward. For any member package that lacks its
own `[tool.pytest.ini_options]`, the rootdir escapes to the workspace root's
`pyproject.toml`, which loads the workspace-root `conftest.py`. In a monorepo
whose root conftest declares shared plugins (e.g.
`pytest_plugins = ["tests.pg_fixtures"]`) and imports sibling packages, every
such member job fails with a confusing cross-workspace import error
(`ImportError: Error importing plugin ...: No module named '<sibling>'`) — even
though the member's tests are fine when run with the correct rootdir.

Empirically confirmed in a 25-member workspace: exactly the members missing
their own pytest config failed; members with `[tool.pytest.ini_options]`
resolved rootdir to themselves and passed.

## Expected behavior

The router's per-member pytest invocation should pin the rootdir to the member
(e.g. `uv run pytest --rootdir . tests/` or `-c pyproject.toml`), OR the
scaffold/check system should hard-error when a member lacks its own pytest
config while the workspace root has a conftest — silent escape into another
package's test infrastructure is exactly the silent-degradation class the tools
exist to prevent.

## Notes

Fixing the invocation is necessary but may not be sufficient for members with
genuine cross-member test coupling (upward test imports, undeclared deps) —
those surface as honest failures once the rootdir is pinned, which is the
desired behavior.

## Affected

- `rlsbl/commands/monorepo/sync.py::_generate_router` (job template)
- Possibly a new workspace check ("member-pytest-config") if the hard-error
  route is chosen.

## Effort

S for the rootdir pin; M with the check + tests.
