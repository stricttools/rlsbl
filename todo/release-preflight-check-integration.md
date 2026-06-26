# Release flow should run checks as pre-flight, not reimplement them

## Problem

The release flow (`rlsbl monorepo release run`) and the check system (`rlsbl check`) validate the same things but through different code paths. The core functions are shared (`lint_library()`, `run_project_tests()`), but the **wrapper code** that calls them diverges — different parameter threading, different target selection logic, different changelog validation. This means `rlsbl check` can pass while `rlsbl monorepo release run` fails on the same project, which is what happened 5 times in a row on the www monorepo's auth releasable.

## Evidence: 5 consecutive release failures that checks didn't catch

1. **Test runner wrong registry.** `run_releasable_tests()` in hooks.py took `targets[0].name` without filtering for recognized types. The check system's `check_test_suite()` in quality.py filters for `{pypi, go, npm, maven}` first. Same shared `run_project_tests()` underneath, different target selection above it. Fixed in 0.86.0.

2. **Non-workspace member test runner.** `_run_pypi_tests()` assumed all pypi packages are workspace members. The check system happened to work because it runs from the package directory. The release flow runs from the workspace root. Fixed in 0.87.0 by probing where pytest is declared.

3. **lint_allow not passed in release flow.** `run_releasable_lint()` in hooks.py hardcoded `allowed_imports=None`. The check system's `check_library_lint()` in quality.py correctly read `proj.get("lint_allow")`. Same `lint_library()` call, different parameter threading in the wrapper. Fixed in 0.88.1.

4. **Unreachable-code false positives.** `_check_unreachable_code` in python_ast.py flags `return` inside `if` blocks as making subsequent code unreachable. This affects both systems equally (shared code), but it was only discovered during release because nobody runs `rlsbl check --tag quality` routinely on every package before releasing. A pre-flight check would have caught this earlier. Pending fix.

## Root cause

The release flow reimplements validation instead of delegating to the check system:

- **Lint wrappers:** hooks.py has `run_releasable_lint()` which builds its own lookup of library flags and lint_allow from workspace projects, then calls `_run_builtin_lint()`. The check system's `check_library_lint()` does the same thing differently. Two wrappers, one shared function, divergent parameter handling.

- **Test wrappers:** hooks.py has `run_releasable_tests()` which iterates members and detects targets. The check system's `check_test_suite()` does the same thing differently. The target filtering bug existed only in the release wrapper.

- **Changelog validation:** Fully parallel implementations. The check system has 10+ granular checks in checks/changelog.py. The release flow has a monolithic `validate_changelog_state()` in validate.py. They don't share code at all.

- **No pre-flight step:** The release flow never calls `rlsbl check` before starting irreversible work (version bumps, commits, tags, pushes). It has its own validation, which is a subset of what checks cover.

## Proposed fix

**Short-term:** Run `rlsbl check --tag release --tag changelog --tag quality` as a pre-flight step before the release flow does any work. If any check fails, abort before version bumps or commits. This catches all check-system-level issues without changing the release flow's own validation.

**Long-term:** The release flow's lint/test/changelog wrappers in hooks.py should be thin delegates that call into the check system's functions rather than reimplementing parameter threading. The check functions already handle workspace project lookup, target detection, and lint_allow correctly. The release flow should reuse that work, not redo it.

## Affected files

- `rlsbl/commands/release/__init__.py` — add pre-flight check step before release execution
- `rlsbl/commands/release/hooks.py` — `run_releasable_lint()` and `run_releasable_tests()` wrappers should delegate to check system logic
- `rlsbl/commands/release/validate.py` — changelog validation should call check functions, not reimplement
- `rlsbl/checks/quality.py` — the source of truth for lint and test validation
- `rlsbl/checks/changelog.py` — the source of truth for changelog validation
