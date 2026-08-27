"""Shared file-walking utilities for linters providing recursive directory traversal with gitignore-aware filtering and extension matching."""

import fnmatch
import os
from pathlib import Path

#: Directory names a LINTER never descends into: virtualenvs, caches, and the
#: build/asset output directories a generated file would otherwise be linted
#: from.  Entries containing ``*`` or ``?`` are fnmatch patterns; the rest are
#: exact names.
#:
#: This set is the linters' judgement, NOT a universal truth about source
#: trees.  ``build``, ``dist``, ``static``, ``public`` and ``assets`` are all
#: legal Go package directories and ordinary Python package names, so a caller
#: whose job is to REWRITE a tree rather than lint it must pass its own set --
#: see ``rlsbl.commands.rewrite.go_module_path``, which sweeps everything
#: except ``vendor/`` and ``.git/``.
LINTER_EXCLUDED_DIRS = frozenset({
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".selfdoc", "_build",
    "static", "public", "assets",
    "*.egg-info",
})

_GLOB_CHARS = ("*", "?", "[")


def _split_name_filters(names):
    """``(exact names, glob patterns)`` from a directory-exclusion set."""
    exact = set()
    globs = []
    for entry in names:
        if any(ch in entry for ch in _GLOB_CHARS):
            globs.append(entry)
        else:
            exact.add(entry)
    return frozenset(exact), tuple(globs)


def walk_source_files(
    project_path: str,
    extensions: tuple[str, ...],
    exclude_patterns: list[str],
    exclude_dirs: list[str] | None = None,
    *,
    excluded_dir_names: frozenset[str] = LINTER_EXCLUDED_DIRS,
) -> list[str]:
    """Walk project directory, return source files matching extensions.

    Args:
        project_path: root of the walk.
        extensions: filename suffixes to collect.
        exclude_patterns: fnmatch patterns applied against relative paths.
        exclude_dirs: directory PATHS (relative to *project_path*) to skip,
            matched by normalized absolute path -- used to exclude sibling
            workspace project directories.
        excluded_dir_names: directory NAMES (or fnmatch patterns) pruned
            anywhere in the tree.  Defaults to :data:`LINTER_EXCLUDED_DIRS`,
            which is what every linter wants; a caller that must visit build
            and asset directories passes its own narrower set explicitly.

    By default (empty exclude_patterns), all files including tests are included.
    """
    exact_excluded, glob_excluded = _split_name_filters(excluded_dir_names)
    # Normalize exclude_dirs to absolute paths for reliable matching.
    normalized_exclude_dirs: frozenset[str] = frozenset()
    if exclude_dirs:
        normalized_exclude_dirs = frozenset(
            os.path.realpath(os.path.join(project_path, d))
            for d in exclude_dirs
        )

    results = []
    for dirpath, dirs, filenames in os.walk(project_path):
        dirs[:] = [
            d for d in dirs
            if d not in exact_excluded
            and not any(fnmatch.fnmatchcase(d, pat) for pat in glob_excluded)
        ]

        # Prune directories that match exclude_dirs (sibling project paths)
        if normalized_exclude_dirs:
            dirs[:] = [
                d for d in dirs
                if os.path.realpath(os.path.join(dirpath, d))
                not in normalized_exclude_dirs
            ]

        # Prune directories that match exclude patterns
        if exclude_patterns:
            rel_dir = os.path.relpath(dirpath, project_path)
            pruned = []
            for d in dirs:
                rel_subdir = os.path.join(rel_dir, d) if rel_dir != "." else d
                # Check if directory itself matches a pattern (e.g. "tests/")
                skip = False
                for pat in exclude_patterns:
                    if pat.endswith("/"):
                        if fnmatch.fnmatch(d, pat.rstrip("/")) or fnmatch.fnmatch(
                            rel_subdir + "/", pat
                        ):
                            skip = True
                            break
                    elif fnmatch.fnmatch(rel_subdir, pat):
                        skip = True
                        break
                if not skip:
                    pruned.append(d)
            dirs[:] = pruned

        for filename in filenames:
            if not any(filename.endswith(ext) for ext in extensions):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, project_path)

            # Check file-level exclude patterns
            if exclude_patterns:
                skip = False
                for pat in exclude_patterns:
                    if pat.endswith("/"):
                        # Directory pattern, already handled above
                        parts = Path(rel).parts
                        dir_name = pat.rstrip("/")
                        if dir_name in parts:
                            skip = True
                            break
                    elif fnmatch.fnmatch(os.path.basename(rel), pat):
                        skip = True
                        break
                    elif fnmatch.fnmatch(rel, pat):
                        skip = True
                        break
                if skip:
                    continue

            results.append(full)
    return results
