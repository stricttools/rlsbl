# strictcli dependency has stale version floor

## Problem

The strictcli dependency in `pyproject.toml` is pinned to `>=0.1.1`. This is functional (resolves to 0.8.3) but inconsistent with the project policy of always using unpinned dependencies. The floor is meaningless since rlsbl certainly requires features added well after 0.1.1.

## What's needed

- Remove the version floor (change `>=0.1.1` to just `strictcli`)
- Verify rlsbl still installs and passes tests with the PyPI version
