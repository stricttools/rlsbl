# Scaffold creates lint configs for all languages unconditionally

## Problem

`shared_template_mappings()` in `rlsbl/targets/base.py` (lines 61-63) returns lint config templates for python, go, and npm regardless of which languages are actually present in the project. This means a Python-only project gets `.rlsbl/lint/go.toml` and `.rlsbl/lint/npm.toml` that are never used.

The runtime lint engine (`rlsbl/lint/__init__.py`, `_detect_languages()`) is already language-aware -- it only lints languages with corresponding manifest files (pyproject.toml, go.mod, package.json). The scaffold should match this intelligence.

## Precedent

The Zig target (`rlsbl/targets/zig.py:152`) overrides `shared_template_mappings()` to conditionally include npm wrapper templates.

## Fix

Filter lint config templates in `shared_template_mappings()` based on detected targets/languages. Only create lint configs for languages that are actually present in the project.

## Affected files

- `rlsbl/targets/base.py` (shared_template_mappings)
- `rlsbl/commands/init_cmd.py` (calls shared_template_mappings at lines 1041, 1671)

## Effort

Small -- the detection logic already exists in the lint engine.
