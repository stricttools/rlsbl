# deps-undeclared check should skip try/except ImportError blocks

## Problem

The `deps-undeclared` check does static analysis of import statements and flags any import from a package not in the project's dependencies. However, it doesn't understand the standard Python pattern for optional dependencies:

```python
try:
    from optional_package import something
except ImportError:
    pass
```

This pattern is legitimate — the import is soft/optional and the code handles the missing dependency gracefully. The check should skip imports inside `try/except ImportError` (or `except (ImportError, ...)`) blocks.

## Reproduction

In shopkeep-core, `core/shopkeep_core/tools/__init__.py` has:

```python
try:
    from shopkeep_crawler.browser_tools import (
        extract_styles,
        extract_text,
        take_screenshot,
    )
    # ... register tools ...
except ImportError:
    pass
```

`shopkeep-crawler` depends on `shopkeep-core` (not the other way around). This is an optional import — if crawler is installed, core registers its tools. The check flags this as "imports shopkeep-crawler but does not declare it as a dependency."

## Expected behavior

The check should detect that the import is inside a `try/except ImportError` block and skip it. Only bare imports (not wrapped in try/except) should be flagged.

## Implementation hint

The check likely uses AST parsing. Extend it to walk the AST and skip `Import`/`ImportFrom` nodes that are inside a `Try` node where any of the `handlers` catch `ImportError`.
