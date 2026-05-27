"""Shared path-filtering utilities for matching commits to projects.

Provides functions to retrieve files changed by a commit, check whether
a file belongs to a project (by path prefix or watch globs), and filter
a set of commits to those touching a specific project's files.
"""

import fnmatch
import subprocess


def get_commit_files(sha):
    """Get the list of files changed by a single commit.

    Returns a list of file paths relative to the repo root, or None on error.
    """
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


def file_matches_project(filepath, project):
    """Check whether a file path belongs to a project.

    A file belongs to a project if:
    - It starts with the project's path prefix, OR
    - It matches any of the project's watch globs
    """
    proj_path = project["path"]
    # Normalize: ensure prefix ends with /
    prefix = proj_path.rstrip("/") + "/"
    if filepath == proj_path or filepath.startswith(prefix):
        return True

    for glob_pattern in project.get("watch", []):
        if fnmatch.fnmatch(filepath, glob_pattern):
            return True

    return False


def filter_commits_for_project(commits, project):
    """Filter commits to only those that touch files belonging to a project.

    Takes a set of commit SHAs and a project dict. For each commit, gets its
    changed files and checks whether any file matches the project (by path
    prefix or watch globs).

    Returns the subset of commits where at least one file belongs to the project.
    """
    filtered = set()
    for sha in commits:
        files = get_commit_files(sha)
        if files is None:
            # Cannot determine files -- include the commit to be safe
            filtered.add(sha)
            continue
        for filepath in files:
            if file_matches_project(filepath, project):
                filtered.add(sha)
                break
    return filtered
