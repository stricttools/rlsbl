"""Shared file-walking utilities for linters and source sweeps: the files git lists for a project (tracked, plus untracked files that are not ignored), filtered by extension, directory name, and pattern."""

import fnmatch
import os
import subprocess
from pathlib import Path

from .. import effects
from ..scratch_dirs import scratch_dir_paths

#: Directory names a LINTER never descends into: virtualenvs, caches, and the
#: build/asset output directories a generated file would otherwise be linted
#: from.  Entries containing ``*`` or ``?`` are fnmatch patterns; the rest are
#: exact names.
#:
#: This set is the linters' judgement, NOT a universal truth about source
#: trees.  ``build``, ``dist``, ``static``, ``public`` and ``assets`` are all
#: legal Go package directories and ordinary Python package names, so a caller
#: whose job is to REWRITE a tree rather than lint it must pass its own set --
#: see ``rlsbl.commands.rewrite.go_module_path``, which declares its own narrow
#: set of names.  The scratch directories are not in here and cannot be opted
#: out of: :func:`walk_source_files` prunes them from every walk, at the root of
#: whatever it was pointed at.
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


class SourceWalkError(Exception):
    """The files of a project could not be listed."""


#: The listing every source walk starts from: tracked files, plus untracked
#: files that are not ignored.  An ignored file -- a third-party clone, a build
#: output, a local-only probe -- is never a project source.
GIT_LIST_ARGS = ("ls-files", "-z", "--cached", "--others", "--exclude-standard")


def git_listed_files(project_path: str) -> list[str]:
    """What ``git ls-files --cached --others --exclude-standard`` lists under
    *project_path*, as paths relative to it, sorted.

    Only regular files are returned: a tracked path deleted from the working
    tree, a submodule's gitlink, and a nested repository (listed as a
    directory) are not files to read.

    A *project_path* that does not exist holds no files, and yields none.

    Raises:
        SourceWalkError: *project_path* is not inside a git work tree, or git
            could not be run.
    """
    if not os.path.isdir(project_path):
        return []
    try:
        result = effects.run(
            ["git", *GIT_LIST_ARGS],
            cwd=project_path, capture_output=True, text=True, check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise SourceWalkError(
            f"{project_path} is not inside a git work tree: rlsbl reads exactly "
            f"the files `git {' '.join(GIT_LIST_ARGS[:1] + GIT_LIST_ARGS[2:])}` "
            f"lists (tracked, plus untracked files that are not ignored), so "
            f"the directory must belong to a git repository"
            + (f" (git said: {detail})" if detail else "")
        ) from exc
    except OSError as exc:
        raise SourceWalkError(
            f"could not run git to list the files of {project_path}: {exc}"
        ) from exc
    listed = set()
    for rel in result.stdout.split("\0"):
        if not rel or rel.endswith("/"):
            continue
        if os.path.isfile(os.path.join(project_path, rel)):
            listed.add(rel)
    return sorted(listed)


def _dir_pattern_excludes(name, rel_subdir, exclude_patterns):
    """Does an exclude pattern take the directory *rel_subdir* out?"""
    for pat in exclude_patterns:
        if pat.endswith("/"):
            if fnmatch.fnmatch(name, pat.rstrip("/")) or fnmatch.fnmatch(
                rel_subdir + "/", pat
            ):
                return True
        elif fnmatch.fnmatch(rel_subdir, pat):
            return True
    return False


def _file_pattern_excludes(rel, exclude_patterns):
    """Does an exclude pattern take the file *rel* out?"""
    for pat in exclude_patterns:
        if pat.endswith("/"):
            if pat.rstrip("/") in Path(rel).parts:
                return True
        elif fnmatch.fnmatch(os.path.basename(rel), pat):
            return True
        elif fnmatch.fnmatch(rel, pat):
            return True
    return False


def walk_source_files(
    project_path: str,
    extensions: tuple[str, ...],
    exclude_patterns: list[str],
    exclude_dirs: list[str] | None = None,
    *,
    excluded_dir_names: frozenset[str] = LINTER_EXCLUDED_DIRS,
) -> list[str]:
    """Return the project's source files matching *extensions*, absolute, sorted.

    The candidates are exactly what :func:`git_listed_files` lists: tracked
    files plus untracked files that are not ignored.  An ignored file is never
    returned, whatever the filters below say, and a directory outside any git
    work tree is refused (:class:`SourceWalkError`).  The filters then narrow
    that listing:

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

    *project_path*'s own scratch directories (:data:`~rlsbl.scratch_dirs.
    SCRATCH_DIR_NAMES`) are always pruned, whatever the caller passes: they hold
    throwaway probes and produced repositories, which are never this project's
    sources.  Only the ones at *project_path* itself are pruned -- a directory
    of the same name nested deeper is an ordinary source directory.
    """
    exact_excluded, glob_excluded = _split_name_filters(excluded_dir_names)
    # Normalize exclude_dirs to absolute paths for reliable matching, and add
    # the project's own scratch directories: they hold throwaway probes and
    # produced repositories, which no walk over the project's sources may see.
    normalized_exclude_dirs: frozenset[str] = scratch_dir_paths(project_path)
    if exclude_dirs:
        normalized_exclude_dirs |= frozenset(
            os.path.realpath(os.path.join(project_path, d))
            for d in exclude_dirs
        )

    # Each directory's verdict, computed once however many files it holds.
    dir_excluded: dict[str, bool] = {}

    def _excluded_dir(rel_subdir):
        verdict = dir_excluded.get(rel_subdir)
        if verdict is None:
            name = os.path.basename(rel_subdir)
            verdict = (
                name in exact_excluded
                or any(fnmatch.fnmatchcase(name, pat) for pat in glob_excluded)
                or os.path.realpath(os.path.join(project_path, rel_subdir))
                in normalized_exclude_dirs
                or (bool(exclude_patterns)
                    and _dir_pattern_excludes(name, rel_subdir, exclude_patterns))
            )
            dir_excluded[rel_subdir] = verdict
        return verdict

    results = []
    for rel_posix in git_listed_files(project_path):
        filename = rel_posix.rsplit("/", 1)[-1]
        if not any(filename.endswith(ext) for ext in extensions):
            continue
        parts = rel_posix.split("/")
        if any(
            _excluded_dir("/".join(parts[:i + 1]))
            for i in range(len(parts) - 1)
        ):
            continue
        rel = os.path.join(*parts)
        if exclude_patterns and _file_pattern_excludes(rel, exclude_patterns):
            continue
        results.append(os.path.join(project_path, rel))
    return results
