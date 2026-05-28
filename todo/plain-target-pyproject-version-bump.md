# Plain target should bump pyproject.toml version if present

## Problem

The `plain` release target only bumps `VERSION`. But some plain-target projects also have a `pyproject.toml` (e.g., an internal CLI tool that's installed locally but never published to PyPI). After a release, `VERSION` says `0.5.0` but `pyproject.toml` still says `0.4.1`.

## Suggestion

During the version-bump step, if the target is `plain` and a `pyproject.toml` exists with a `[project] version` field, bump it too. This doesn't imply publishing to PyPI — it just keeps the metadata consistent. The plain target already bumps `VERSION`; this extends it to also bump `pyproject.toml` when present.

## Current workaround

Manual sync or a pre-release hook with `sed`. Neither is great.
