# Dead module detection: false positives for non-import consumption patterns

## Problem

`rlsbl check` flags files as "dead modules" when they are consumed by non-import mechanisms. A consumer project currently has 24 false positives:

- **21 JS files** in a `js/` package subdirectory, loaded at runtime via `importlib.resources` (Python's standard mechanism for loading package data files). These are not Python modules and are never imported -- they are data resources bundled inside the package and read as file content at build time.

- **3 Python scripts** in `scripts/`, executed by a gen_data system that runs them via `subprocess` inside bubblewrap-sandboxed environments. They are standalone scripts invoked as subprocesses, not imported by any module.

## Why these are not dead

### importlib.resources

`importlib.resources` is the standard Python mechanism for accessing non-code files bundled inside packages. The JS files are package data: they live inside the Python package directory, are included in the wheel, and are read at runtime using `importlib.resources.files()`. They have no `__init__.py` semantics and are never on any import path. Flagging them as dead modules is a category error -- they are data, not modules.

### gen_data subprocess execution

The gen_data system runs scripts via `subprocess.run()` inside bubblewrap (`bwrap`) sandboxed environments. The scripts are standalone executables, not importable modules. They generate data files consumed by the build pipeline. There is no Python import relationship between the caller and these scripts -- the relationship is subprocess invocation.

## What should change

The dead module detection should understand these non-import consumption patterns:

1. **importlib.resources**: Files loaded via `importlib.resources` (or `pkg_resources`, or direct file reads from package directories) should not be flagged as dead modules. These are data resources, not code modules.

2. **Subprocess-executed scripts**: Scripts in conventional locations (e.g., `scripts/`) that are invoked via `subprocess` should not be flagged as dead modules. They are executables, not importable modules.

The detection could either:
- Scan for `importlib.resources.files()` calls and resolve which package directories they reference
- Allow projects to declare non-import consumption patterns in config (e.g., a `data_packages` or `subprocess_scripts` field)
- Exclude known non-module file extensions (`.js`, `.css`, `.html`, etc.) from dead module detection entirely
