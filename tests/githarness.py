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

import os
import subprocess

# The stricttest floor exports a throwaway commit identity in the ENVIRONMENT as
# well as in the throwaway global git config, so a git invocation that ignores
# the config file still cannot commit as the real developer. Environment
# identity outranks every config level, including a repo's own
# ``git config user.name`` -- which would make ``init_repo``'s ``name=`` /
# ``email=`` arguments silently inert. This harness only ever touches throwaway
# repos whose identity it just declared, so it drops the ambient identity and
# lets that declaration win. Nothing is weakened: with the vars unset and no
# repo-local identity, git falls back to the floor's throwaway global config.
_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


def harness_env():
    """The process environment minus the floor's ambient commit identity."""
    return {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}


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
        env=harness_env(),
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


def fake_run_dispatch(*, head_sha="abc123def456", toplevel="/tmp/fake-repo",
                      porcelain="", porcelain_after_bump=None,
                      remote_head=None, log_subject="", behind_count="0",
                      branch="main"):
    """Command-dispatching stand-in for ``rlsbl.commands.release.run``.

    Release-flow unit tests used to feed ``mock_run.side_effect`` a positional
    list of canned outputs, one per expected ``run()`` call. Any change to the
    order or number of git calls inside the release flow silently exhausted the
    list and produced a ``StopIteration`` unrelated to the behaviour under
    test. This helper answers by COMMAND instead, so a test asserts what it
    means to assert.

    ``porcelain`` is returned by ``git status`` until the release captures its
    pre-release HEAD (``git rev-parse HEAD``); after that point
    ``porcelain_after_bump`` (defaulting to ``porcelain``) is returned, which is
    what the unexpected-modified-files re-check guard observes.
    """
    seen_head = {"v": False}
    after = porcelain if porcelain_after_bump is None else porcelain_after_bump

    def fake(cmd, args=None, timeout=None, env=None, cwd=None, **kwargs):
        a = list(args or [])
        if cmd != "git" or not a:
            return ""
        # ``--no-optional-locks`` is a GLOBAL git option, so it sits before the
        # subcommand at every call site that reads the worktree or the index.
        if a[0] == "--no-optional-locks":
            a = a[1:]
            if not a:
                return ""
        if a[0] == "status":
            return after if seen_head["v"] else porcelain
        if a[0] == "rev-parse":
            if "--show-toplevel" in a:
                return toplevel
            if a[1:2] == ["HEAD"]:
                seen_head["v"] = True
            return head_sha
        if a[0] == "ls-remote":
            head = head_sha if remote_head is None else remote_head
            return f"{head}\trefs/heads/{branch}" if head else ""
        if a[:2] == ["rev-list", "--count"]:
            return behind_count
        if a[0] == "log":
            return log_subject
        return ""

    return fake
