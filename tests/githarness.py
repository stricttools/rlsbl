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
from pathlib import Path

from rlsbl import effects as _effects

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


def write_covered_unreleased(root, *, description="test", entry_type="feature",
                             changes_dir=None, scope_path=None):
    """Write an ``unreleased.jsonl`` whose commit hash really resolves.

    The changelog validators are PURE, so ``release run --dry-run`` executes
    them for real.  A fixture carrying an unresolvable placeholder hash
    (``abc1234``) therefore aborts the preview on ``changelog-hashes`` before
    it ever reaches the behaviour under test -- which is the correct product
    behaviour and the wrong fixture.

    Initializes a throwaway git repo at *root* when there is not one already,
    commits whatever the fixture has written so far, and covers that commit.
    Returns the covered hash.

    *changes_dir* overrides where the file lands (monorepo sub-projects and
    releasables keep their changes elsewhere); it defaults to
    ``<root>/.rlsbl/changes``.
    """
    import json as _json
    import os as _os

    root = Path(root)
    if not (root / ".git").is_dir():
        init_repo(root)
    # Everything the fixture has written so far becomes one covered commit.
    # Paths are enumerated and passed explicitly -- never `git add -A`.
    staged = []
    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            staged.append(
                _os.path.relpath(_os.path.join(dirpath, name), str(root))
            )
    if not staged:
        (root / ".fixture-seed").write_text("seed\n")
        staged = [".fixture-seed"]
    git(root, "add", "--", *staged)
    if git(root, "status", "--porcelain", check=False):
        git(root, "commit", "-q", "-m", "fixture")
    sha = git(root, "rev-parse", "HEAD")

    # Cover EVERY commit in the unreleased range, not just HEAD: coverage is
    # per commit, so a fixture with two commits and one entry fails
    # changelog-coverage instead of reaching the behaviour under test.
    #
    # ``scope_path`` narrows the range the way a monorepo sub-project's
    # coverage is narrowed: an entry naming a commit that never touched the
    # sub-project is out of range rather than helpful.
    # The range is the one PRODUCTION computes: bounded by the release the
    # RELEASE RECORD anchors this checkout to, not by `git describe`. A fixture that
    # tags v1.0.0 without archiving it has released nothing as far as rlsbl is
    # concerned, so every commit needs covering -- and covering the wider range
    # is always safe.
    from rlsbl.release_record import releases_dir_for_changes_dir, unreleased_range

    _changes = Path(changes_dir) if changes_dir else root / ".rlsbl" / "changes"
    rev_range = unreleased_range(
        releases_dir_for_changes_dir(str(_changes)), cwd=str(root),
    )
    args = ["rev-list", rev_range]
    if scope_path is not None:
        args += ["--", str(scope_path)]
    commits = [line for line in git(root, *args).splitlines() if line]
    if sha not in commits:
        commits.insert(0, sha)

    target = Path(changes_dir) if changes_dir else root / ".rlsbl" / "changes"
    _os.makedirs(target, exist_ok=True)
    lines = [
        _json.dumps({
            "format_version": 1,
            "commits": [sha],
            "user_facing": True,
            "description": description,
            "type": entry_type,
        })
    ]
    lines += [
        _json.dumps({
            "format_version": 1, "commits": [c], "user_facing": False,
        })
        for c in commits if c != sha
    ]
    (target / "unreleased.jsonl").write_text("\n".join(lines) + "\n")
    return sha


def record_release(repo, tag, *, release_record=None, commit=True, sha=None):
    """Tag a release AND record it in the RELEASE RECORD the range is measured from.

    Creating a git tag no longer makes a version released as far as rlsbl is
    concerned: the unreleased range is bounded by the highest ARCHIVED release
    the checkout contains, so a fixture that only tags has released nothing and
    every commit in it reads as unreleased.

    The archive directory is resolved from the tag's scheme unless *release record*
    says otherwise:

    * ``v1.2.3``            -> ``<repo>/.rlsbl/releases``
    * ``name@v1.2.3``       -> the releasable's ``releases/`` when
      ``.rlsbl-monorepo/releasables/<name>/`` exists, else
      ``<repo>/<name>/.rlsbl/releases`` when that package directory exists,
      else the repo's own.
    * ``some/path/v1.2.3``  -> ``<repo>/some/path/.rlsbl/releases``

    With *commit* the archive is committed with an ``Autogenerated`` trailer,
    which is also what keeps it out of changelog coverage. Returns the archived
    version string.
    """
    import os as _os

    from rlsbl.release_file import write_archived_release_file
    from rlsbl.tag_glob import TagMode, parse_version_tag

    repo = Path(repo)
    parsed = parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE)
    if parsed is None:
        raise AssertionError(f"not a version tag: {tag!r}")
    version = parsed.version

    git(repo, "tag", tag)
    head = sha or git(repo, "rev-parse", "HEAD")

    if release_record is None:
        if parsed.scheme == "monorepo":
            name = tag.rsplit("@", 1)[0]
            rel = repo / ".rlsbl-monorepo" / "releasables" / name
            pkg = repo / name
            if rel.is_dir():
                release_record = rel / "releases"
            elif pkg.is_dir():
                release_record = pkg / ".rlsbl" / "releases"
            else:
                release_record = repo / ".rlsbl" / "releases"
        elif parsed.scheme == "path":
            release_record = repo / tag.rsplit("/", 1)[0] / ".rlsbl" / "releases"
        else:
            release_record = repo / ".rlsbl" / "releases"

    _os.makedirs(str(release_record), exist_ok=True)
    write_archived_release_file(
        str(release_record), version,
        bump="patch", include=[], description=f"release {version}",
        candidate_sha=head, tree_hashes={".": git(repo, "rev-parse", f"{head}^{{tree}}")},
    )
    if commit:
        rel_path = _os.path.relpath(str(release_record), str(repo))
        git(repo, "add", "--", rel_path)
        if git(repo, "status", "--porcelain", check=False):
            git(repo, "commit", "-q", "-m", f"chore: archive release {version}",
                "--trailer", "Autogenerated: true")
    return version


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

    def porcelain_now():
        """The canned working-tree answer at this point in the flow.

        Exposed on the returned stand-in so a test can answer the SHARED
        working-tree read (``rlsbl.utils.working_tree_paths`` and the release
        executor's declared status capture, both of which go through
        ``rlsbl.effects.run``) from the same canned text -- see
        :func:`status_answering_effects_run`.
        """
        return after if seen_head["v"] else porcelain

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
            return porcelain_now()
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

    fake.porcelain_now = porcelain_now
    fake.head_sha = head_sha
    fake.toplevel = toplevel
    return fake


# The real effects entry point, captured before any test patches it.
_REAL_EFFECTS_RUN = _effects.run


def _z_records(porcelain):
    """The NUL-terminated records git emits for *porcelain* under ``-z``."""
    return "".join(
        line + "\0" for line in porcelain.splitlines() if line.strip()
    )


def canned_status_effects_run(porcelain=""):
    """``rlsbl.effects.run`` stand-in answering ONLY the working-tree read.

    rlsbl reads the working tree in one form -- ``git status --porcelain -z``,
    issued on the effects chokepoint by ``rlsbl.utils.working_tree_status`` and
    by the release executor's declared status capture. A test that fakes git by
    patching ``rlsbl.commands.release.run`` never sees that read, so in a real
    fixture repository the release observes the fixture's own uncommitted files
    and its concurrent-change guard refuses.

    This answers that one read from *porcelain* (default: a clean tree) and
    delegates everything else -- every other git call, every non-git call -- to
    the real ``effects.run``.
    """
    def fake(argv, **kwargs):
        a = list(argv)
        if a[:1] == ["git"] and "status" in a and "-z" in a:
            return subprocess.CompletedProcess(a, 0, _z_records(porcelain), "")
        return _REAL_EFFECTS_RUN(argv, **kwargs)

    return fake


def status_answering_effects_run(run_fake):
    """Stand-in for ``rlsbl.effects.run`` that answers git from *run_fake*.

    A release-flow test fakes git by patching ``rlsbl.commands.release.run``,
    but two reads do not go through it: the shared working-tree read
    (``rlsbl.utils.working_tree_status``, issued as ``git status --porcelain
    -z``) and the release executor's declared result captures. Both are issued
    on the effects chokepoint, and in a fixture directory that is not a git
    repository the real ones fail.

    So every ``git`` argv is answered by *run_fake* (a
    :func:`fake_run_dispatch` stand-in) and wrapped in a
    ``CompletedProcess``; the ``-z`` working-tree read gets the canned
    porcelain converted into the NUL-terminated records git emits under that
    flag. Everything that is not git is delegated to the real ``effects.run``.

    Usage::

        run_fake = fake_run_dispatch(porcelain=" M notes.txt")
        with patch("rlsbl.commands.release.run", side_effect=run_fake), \\
             patch("rlsbl.effects.run",
                   side_effect=status_answering_effects_run(run_fake)):
            ...
    """
    def fake(argv, **kwargs):
        a = list(argv)
        if not a or a[0] != "git":
            return _REAL_EFFECTS_RUN(argv, **kwargs)
        if "status" in a and "-z" in a:
            stdout = _z_records(run_fake.porcelain_now())
        elif "rev-parse" in a and a[-1] in ("HEAD", "--show-toplevel"):
            # The executor's declared HEAD capture and the git-root lookup,
            # answered from the canned values DIRECTLY. Asking *run_fake* would
            # advance its pre/post-bump flip, which belongs to the release
            # flow's own ``run`` calls and not to this stand-in. A rev-parse of
            # anything else is a hash resolution the changelog validators own,
            # and is left to the real read.
            stdout = (
                run_fake.toplevel if a[-1] == "--show-toplevel"
                else run_fake.head_sha
            ) + "\n"
        else:
            # Everything else is somebody else's read (the changelog
            # validators resolve real hashes, for one): leave it alone.
            return _REAL_EFFECTS_RUN(argv, **kwargs)
        return subprocess.CompletedProcess(a, 0, stdout, "")

    return fake
