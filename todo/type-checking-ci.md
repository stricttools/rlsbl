# Add mypy/pyright to CI

## Problem

Type hints exist throughout the codebase but are never validated. No mypy or pyright configuration exists. Type errors accumulate silently.

## Suggested approach

- Add `pyright` or `mypy` configuration (pyproject.toml)
- Start with strict mode on new modules, gradual adoption for existing ones
- Add to CI as a non-blocking check initially, then make it blocking

## Origin

From the hardening roadmap (v0.69-v0.70 investigation), architecture debt section.
