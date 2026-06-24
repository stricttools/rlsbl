# TYPE_CHECKING imports not detected by AST scanner

## Problem

`deps-undeclared` reports false positives for imports inside `if TYPE_CHECKING:` blocks. These are type-only imports that never execute at runtime — they should not be treated as runtime dependencies.

Example: a protocols module has `from orxtra.session import Session` inside `if TYPE_CHECKING:` for use in a Protocol type annotation. The import scanner flags this as an undeclared runtime dependency, but adding session as a dependency would create incorrect coupling (protocols should not depend on session at runtime).

## Root cause

`_collect_all_imports` in `lint/python_ast.py` detects `try/except ImportError` guards (via `_is_in_try_except_import_error`) but does not detect `if TYPE_CHECKING:` blocks. Imports inside TYPE_CHECKING are classified as `guarded=False` and treated as runtime imports.

This was specced in `todo/.done/dependency-import-validation.md` (line 44): "Skip `if TYPE_CHECKING:` imports (typing-only, not runtime deps)." The spec was implemented for try/except but not for TYPE_CHECKING.

## Fix

Add TYPE_CHECKING detection to `_collect_all_imports` in `lint/python_ast.py`. The tree-sitter walker should detect `if_statement` nodes whose condition is `TYPE_CHECKING` (or `typing.TYPE_CHECKING`) and either mark imports inside them as `guarded=True` or add a `type_checking=True` field. The import scanner in `import_scanners.py` should exclude TYPE_CHECKING imports from undeclared-deps checking.
