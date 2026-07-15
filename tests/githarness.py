"""Shared real-git test harness.

Module-level helpers (not fixtures) for building throwaway git repositories,
bare remotes, and commits in tests that exercise real git behavior. Import
these directly:

    from githarness import init_repo, commit_file, add_remote

The goal is that a test can stand up a repo plus a bare remote in five lines
or fewer:

    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "README.md", "# hi\\n", "initial")
    add_remote(repo, tmp_path / "remote")

These consolidate helpers that were previously duplicated across the
real-git integration test modules. The canonical git runner returns the
command's stdout (stripped) and supports ``check=False`` for commands that
may legitimately fail (e.g. ``git show`` of a path that does not exist at a
given revision).
"""

import subprocess


def git(repo, *args, check=True):
    """Run a git command in ``repo`` and return its stdout, stripped.

    ``check`` mirrors ``subprocess.run``: when False, a non-zero exit does
    not raise and the (typically empty) stdout is returned instead.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def init_repo(repo, *, email="test@test.local", name="Test", branch="main"):
    """Create ``repo`` (if needed) and initialize a git repo with identity.

    Creates the directory (parents included), runs ``git init`` on the given
    default ``branch``, and configures user.email / user.name so commits
    succeed without relying on global git config.
    """
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", email)
    git(repo, "config", "user.name", name)


def commit_file(repo, relpath, content, message):
    """Write ``content`` to ``relpath`` under ``repo``, add, and commit.

    Creates parent directories as needed. Returns the new HEAD hash.
    """
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", relpath)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def add_remote(repo, remote_dir, *, name="origin", branch="main", push=True):
    """Create a bare remote at ``remote_dir`` and wire it up to ``repo``.

    Initializes a bare repository at ``remote_dir``, adds it as remote
    ``name``, and (by default) pushes ``branch`` plus all tags so the remote
    starts in sync with the local repo.
    """
    remote_dir.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "--bare"],
        cwd=str(remote_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    git(repo, "remote", "add", name, str(remote_dir))
    if push:
        git(repo, "push", "-q", name, branch, "--tags")


def remote_ref(repo, refname, *, remote="origin"):
    """Return the hash a remote ref points to, or "" if it does not exist."""
    out = git(repo, "ls-remote", remote, refname)
    parts = out.split()
    return parts[0] if parts else ""


def snapshot_remote_refs(repo, *, remote="origin"):
    """Snapshot all of ``remote``'s refs as a {refname: hash} dict."""
    refs = {}
    for line in git(repo, "ls-remote", remote).splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    return refs
