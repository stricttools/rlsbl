# Remaining loose ends

## From hardening roadmap (now in .done/)

### Path normalization consistency (H1)

Mixed `os.path.realpath`, `os.path.abspath`, `os.path.normpath` across the codebase, zero `Path.resolve()`. Root cause of 11 historical bug fixes. A symlink edge case can still reproduce the same bug class. `dep_validation.py` is the worst offender (77 `os.path` calls with mixed strategies). `workspace.py` uses `realpath` while most other modules use `abspath`.

### Cross-project batch limits (H3)

Batch size checks in `rlsbl/changelog/validate.py` run per-project independently. A commit appearing in multiple workspace projects' changelog entries is invisible to the check. The other two changelog bugs from the roadmap (exclusion hash matching, partial orphan messaging) were fixed.

## Small items from v0.74.1/v0.75.0 session

### _classify_variant_collisions unit test

User approved adding a focused unit test verifying `_classify_variant_collisions` calls `_ultranormalize` correctly for the PyPI pairwise comparison path. The integration tests in `TestCheckNameEndToEnd` cover this end-to-end, but a dedicated unit test pairing the two functions is missing.

### _update_last_build_release missing

The Plan A audit (release-py-split) found that `_update_last_build_release` is not in the release package. Needs investigation: intentionally removed, moved to another module, or dropped by accident.
