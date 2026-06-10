"""Shared path-filtering utilities for matching commits to projects.

Provides functions to retrieve files changed by a commit, check whether
a file belongs to a project (by path prefix or watch globs), filter
a set of commits to those touching a specific project's files,
validate SSH host consistency between origin and subtree remotes,
and detect manual pushes to release branches.
"""

import fnmatch
import os
import re
import subprocess
import sys


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


# -- SSH host validation for subtree remotes ---------------------------------

# Matches git@HOST:owner/repo.git (SCP-like syntax)
_SCP_RE = re.compile(r"^[^@]+@([^:]+):")

# Matches ssh://git@HOST/owner/repo.git
_SSH_URL_RE = re.compile(r"^ssh://[^@]+@([^/]+)")


def extract_ssh_host(git_url):
    """Extract the SSH host from a git URL.

    Supports SCP-like syntax (git@host:owner/repo.git) and explicit SSH URLs
    (ssh://git@host/owner/repo.git). Returns the host string, or None for
    HTTPS URLs, empty strings, or unparseable formats.
    """
    if not git_url:
        return None

    # Check ssh:// scheme first to avoid the SCP regex matching it
    m = _SSH_URL_RE.match(git_url)
    if m:
        return m.group(1)

    m = _SCP_RE.match(git_url)
    if m:
        return m.group(1)

    return None


def validate_subtree_remote_ssh_host(subtree_remote, project_root):
    """Validate that subtree_remote and origin use the same SSH host.

    Hard error (sys.exit(1)) when both URLs are SSH and their hosts differ.
    Silently passes when either URL is not SSH or when origin cannot be read.
    """
    # Read origin URL
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
        if result.returncode != 0:
            return
        origin_url = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return

    if not origin_url:
        return

    origin_host = extract_ssh_host(origin_url)
    subtree_host = extract_ssh_host(subtree_remote)

    # Only validate when both are SSH
    if origin_host is None or subtree_host is None:
        return

    if origin_host != subtree_host:
        print(
            f"Error: subtree_remote uses SSH host '{subtree_host}' "
            f"but origin uses '{origin_host}'. "
            f"Use a URL with host '{origin_host}' instead.",
            file=sys.stderr,
        )
        sys.exit(1)


def detect_manual_push_branches(stdin_lines, release_branches):
    """Return list of release branch names being pushed to manually.

    Parses pre-push hook stdin lines for ``refs/heads/<branch>`` patterns
    and returns branch names that match *release_branches*.

    Returns an empty list if no release branches are being pushed to,
    if *stdin_lines* is empty/None, or if ``RLSBL_RELEASE_PUSH`` is set
    (indicating a legitimate release push).
    """
    if os.environ.get("RLSBL_RELEASE_PUSH") == "1":
        return []
    if not stdin_lines:
        return []

    pushed = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_ref = parts[0]
        if not local_ref.startswith("refs/heads/"):
            continue
        branch_name = local_ref[len("refs/heads/"):]
        if branch_name in release_branches:
            pushed.append(branch_name)
    return pushed
