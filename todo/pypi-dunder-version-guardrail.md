# PyPI target: hard error when __init__.py exists but has no __version__

## Problem

`_update_dunder_version` in `rlsbl/targets/pypi.py` silently returns `None` when a pypi target's `__init__.py` exists but contains no `__version__` line. The caller (`write_version`) treats this as "nothing to update" and proceeds. The release succeeds, but any version constant in the file (under a different name) is never bumped.

This caused a real bug in pgdesign: the PyPI wrapper had `VERSION = "0.1.0"` (not `__version__`), so rlsbl never bumped it. The constant was used to construct a GitHub Release download URL, which 404'd on every version after 0.1.0. The bug survived 14 releases undetected because rlsbl silently skipped the file.

## Why this is wrong

The current behavior is silent degradation — the exact pattern the project principles forbid. If `__init__.py` exists in a pypi target's package directory, the overwhelmingly likely intent is that it contains a version constant. Silently skipping it when the regex doesn't match hides misconfiguration.

## Fix

In `_update_dunder_version` (~line 118), when the file exists and is read but `new_content == content` (regex didn't match), raise a hard error instead of falling through to `break` and returning `None`.

The error message should be clear:

> "{pkg_name}/__init__.py exists but contains no `__version__` line. rlsbl expects `__version__ = "x.y.z"` in pypi target packages. Rename your version constant to `__version__` or remove the file if it doesn't need a version."

The "no `__init__.py` found at all" path should remain as `None` (legitimately optional — some packages don't have one, or use a different structure).

## What changed on the pgdesign side

pgdesign is fixing this independently:
- `VERSION` renamed to `__version__` (so rlsbl's regex matches)
- Download URL now uses `importlib.metadata.version("pgdesign")` instead of the constant (eliminates sync dependency)

The rlsbl guardrail prevents the same class of bug in any future pypi target project.

## Tests

- Existing test `test_init_py_without_dunder_version_unchanged` (~line 193) and `test_pypi_returns_only_pyproject_when_init_has_no_dunder` (~line 920) verify the current silent behavior. These should be updated to expect the hard error instead.
- Add a test for the "no `__init__.py` at all" path to verify it still returns `None` without error.
