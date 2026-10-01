"""The release checkout: the detached checkout of the committed commit a release runs in.

A release never runs in the working tree the operator (or another session)
edits. Everything it does before its first push -- the producers (the strictcli
schema dump, selfdoc generation, the pre-checks and pre-release hooks), the
tests and checks, the version bump and the release commit -- happens in a
checkout of the commit the release branch points at, kept at a stable path
under the repository's git common directory::

    <git common dir>/rlsbl/release-checkout

It is one ``git worktree`` of the repository, detached, and it is REUSED from
release to release: each release resets it to the commit it starts from and
removes its untracked files (``git clean -ffd``), leaving ignored files in
place, so dependency environments and build caches stay warm. A history
rewrite (``release scrub``) removes it first, since its HEAD keeps the old
history reachable; the next release creates it afresh. Submodules are
initialized when the commit declares any. The checkout's commits reach the
release branch through one door, :func:`advance_live_branch`:

- the branch advances only by compare-and-swap (``git update-ref <branch> <new>
  <old>``) from the commit this release last left it at, so a branch another
  session moved is refused, never overwritten;
- only the files the release's own commits changed are written into the live
  working tree (``git restore --source=<new> --staged --worktree`` on those
  paths alone), and only when every one of them is clean there -- an
  uncommitted edit to one of them refuses the advance, naming it;
- every other file in the live working tree is never read and never written,
  so another session's uncommitted work neither blocks a release nor leaks
  into it.

A release that fails before its first push discards the checkout's commits:
the next release resets the checkout anyway, and nothing of this one reached
the branch or the working tree. The one live-tree write that precedes a push is
the advance immediately before the candidate push; when that push is refused,
:func:`unwind_last_advance` takes it back, again by compare-and-swap and again
only for files that still hold what the release wrote. There is no
``git reset --hard`` anywhere in the release.

State that belongs to the operator's working tree rather than to the committed
commit stays there: the release state file (``in-progress.json``), the
advisory lock, the ``env_file``, and the ``dev-sources.toml.local-only``
overlays the version-skew guard reads. :func:`live_path` maps a checkout path
back to its live counterpart for those readers.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sys

from . import effects
from .errors import GitError, RlsblError

#: The checkout's location, relative to the repository's git common directory.
CHECKOUT_RELATIVE = os.path.join("rlsbl", "release-checkout")
#: The release's directory for binaries, relative to the git common directory.
RELEASE_BIN_RELATIVE = os.path.join("rlsbl", "release-bin")
#: The variable naming that directory to the release's hooks.
RELEASE_BIN_ENV = "RLSBL_RELEASE_BIN"


class ReleaseCheckoutError(RlsblError):
    """The release checkout could not be prepared or used."""


class LiveTreeConflictError(RlsblError):
    """Paths the release writes have uncommitted changes in the working tree."""


class BranchMovedError(RlsblError):
    """The release branch is not where this release last left it."""


@dataclasses.dataclass
class ReleaseCheckout:
    """One release's checkout and the live repository it releases."""

    #: The live working tree's root (realpath).
    live_root: str
    #: The checkout's root (realpath).
    path: str
    #: The release branch the checkout's commits advance.
    branch: str
    #: The commit the live branch is expected at: the commit the release
    #: started from, then each commit :func:`advance_live_branch` moved it to.
    tip: str
    #: The most recent advance as ``(old, new, paths)``, for
    #: :func:`unwind_last_advance`. None once unwound, or before any advance.
    last_advance: tuple | None = None

    def owns(self, path) -> bool:
        """True when *path* lies inside this checkout."""
        real = os.path.realpath(os.fspath(path))
        return real == self.path or real.startswith(self.path + os.sep)

    def to_live(self, path) -> str:
        """The live counterpart of a checkout *path* (other paths unchanged)."""
        if not self.owns(path):
            return os.fspath(path)
        rel = os.path.relpath(os.path.realpath(os.fspath(path)), self.path)
        return self.live_root if rel == "." else os.path.join(self.live_root, rel)

    def to_checkout(self, path) -> str:
        """The checkout counterpart of a live *path* inside the live tree."""
        real = os.path.realpath(os.fspath(path))
        if real != self.live_root and not real.startswith(self.live_root + os.sep):
            raise ReleaseCheckoutError(
                f"{path} is outside the repository at {self.live_root}; the "
                f"release checkout holds nothing for it."
            )
        rel = os.path.relpath(real, self.live_root)
        return self.path if rel == "." else os.path.join(self.path, rel)


_active: ReleaseCheckout | None = None


def active() -> ReleaseCheckout | None:
    """The checkout the running release works in, or None outside one."""
    return _active


def live_path(path):
    """*path* mapped out of the active checkout into the live tree.

    Paths outside the checkout, and every path when no release checkout is
    active, come back unchanged. The readers of operator-owned state (the
    release state file, the lock, the env file, the dev overlays) go through
    here, because none of that state is committed.
    """
    if _active is None or path is None:
        return path
    mapped = _active.to_live(path)
    return type(path)(mapped) if not isinstance(path, str) else mapped


def branch_ref() -> str | None:
    """``refs/heads/<branch>`` of the active checkout's release branch, or None.

    Inside the checkout HEAD is detached and carries only the release's own
    commits, so a question about what reached the BRANCH -- the foreign-commit
    guards ask it -- names the branch ref, which every worktree shares.
    """
    if _active is None:
        return None
    return f"refs/heads/{_active.branch}"


def current_branch_for(cwd) -> str | None:
    """The release branch when *cwd* is inside the active checkout, else None.

    The checkout is a detached checkout OF the release branch; asked from
    inside it, "which branch is this" has one answer.
    """
    if _active is None or cwd is None:
        return None
    return _active.branch if _active.owns(cwd) else None


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(args, *, cwd, check=True, env=None, timeout=300):
    result = effects.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=False, env=env, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"`git {' '.join(args)}` failed in {cwd}: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return result


def _out(args, *, cwd):
    return _git(args, cwd=cwd).stdout.strip()


def _common_dir(root):
    return os.path.realpath(_out(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
    ))


def checkout_dir(live_root) -> str:
    """Where the release checkout of the repository at *live_root* lives."""
    return os.path.join(_common_dir(live_root), CHECKOUT_RELATIVE)


def _literal_env():
    return {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}


def _changed_paths(old, new, *, cwd):
    """Every path *new* changes relative to *old*, renames split in two."""
    out = _git(
        ["diff", "--name-only", "-z", "--no-renames", old, new], cwd=cwd,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _dirty_records(cwd):
    """``(status, path)`` for every change in the working tree at *cwd*.

    ``--no-renames`` reports a staged rename as a deletion plus an addition,
    so both of its paths are named; ``--untracked-files=all`` names every
    untracked file rather than collapsing a directory into one record.
    """
    out = _git(
        ["--no-optional-locks", "status", "--porcelain", "-z",
         "--no-renames", "--untracked-files=all"],
        cwd=cwd,
    ).stdout
    records = []
    for field in out.split("\0"):
        if len(field) >= 4:
            records.append((field[:2], field[3:]))
    return records


def _is_operator_state(path):
    """rlsbl's own untracked bookkeeping, which is never the release's to judge.

    The release state files and the advisory lock sit in the live tree on
    purpose -- a release writes them there -- so the dirty-tree report must not
    count them as anyone's uncommitted work.
    """
    from .commands.release.validate import is_tool_owned_state_path

    parts = path.replace(os.sep, "/").split("/")
    if parts[-1] == "lock" and len(parts) >= 2 and parts[-2] in (
        ".rlsbl", ".rlsbl-monorepo",
    ):
        return True
    return is_tool_owned_state_path(path)


def live_changes(live_root):
    """The live tree's uncommitted changes, minus rlsbl's own bookkeeping."""
    return [
        (status, path) for status, path in _dirty_records(live_root)
        if not _is_operator_state(path)
    ]


def _under(path, scope):
    """True when *path* is *scope* or lies under the directory *scope*."""
    scope = scope.rstrip("/")
    return path == scope or path.startswith(scope + "/")


def partition_changes(changes, write_scope):
    """Split *changes* into ``(blocking, ignored)`` against *write_scope*.

    *write_scope* is the set of repository-relative paths (a directory scope
    covers everything under it) the release writes. A change inside it would
    be overwritten by the release, so it blocks; every other change belongs to
    somebody else and is ignored.
    """
    blocking, ignored = [], []
    for status, path in changes:
        target = blocking if any(_under(path, s) for s in write_scope) else ignored
        target.append((status, path))
    return blocking, ignored


def _render(changes):
    return "\n".join(f"  {status} {path}" for status, path in changes)


def conflict_message(blocking, *, what, rerun):
    """The refusal for uncommitted changes to paths the release writes."""
    return (
        f"{what} writes these paths, and they have uncommitted changes in the "
        f"working tree:\n{_render(blocking)}\n"
        f"Nothing was written to the branch or to the working tree. Commit "
        f"those changes (or take back your own edits to those files), then "
        f"{rerun}."
    )


def report_ignored(ignored, *, log):
    """Say which uncommitted changes the release leaves alone."""
    if ignored:
        log(
            "Uncommitted changes the release does not touch (they stay in the "
            "working tree and are not part of the release):\n"
            + _render(ignored)
        )


# ---------------------------------------------------------------------------
# preparing the checkout
# ---------------------------------------------------------------------------


def _is_this_repositorys_worktree(path, live_root):
    if not os.path.isdir(path):
        return False
    probe = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir",
         "--show-toplevel"],
        cwd=path, check=False,
    )
    if probe.returncode != 0:
        return False
    lines = probe.stdout.strip().splitlines()
    if len(lines) != 2:
        return False
    common, top = (os.path.realpath(line) for line in lines)
    return common == _common_dir(live_root) and top == os.path.realpath(path)


def prepare_checkout(live_root, sha) -> str:
    """Put the release checkout at *sha*, clean, and return its path.

    Created with ``git worktree add --detach`` the first time; afterwards the
    same worktree is reset to *sha* (``checkout --detach --force``) and its
    untracked files removed (``clean -ffd``), keeping ignored files so caches
    stay warm. A directory at the checkout's path that is not this
    repository's release checkout is refused, naming it: rlsbl deletes nothing
    it did not create.
    """
    path = checkout_dir(live_root)
    if os.path.lexists(path):
        if not _is_this_repositorys_worktree(path, live_root):
            raise ReleaseCheckoutError(
                f"{path} exists but is not this repository's release checkout "
                f"(a git worktree of {live_root}). rlsbl creates the release "
                f"checkout there and will not replace a directory it did not "
                f"create. Delete {path} and re-run."
            )
        _git(["checkout", "--detach", "--force", "--quiet", sha], cwd=path)
        _git(["clean", "-ffdq"], cwd=path)
    else:
        effects.makedirs(os.path.dirname(path), exist_ok=True)
        # --force: a registration whose directory is gone (deleted by hand)
        # would otherwise refuse the add; it names only this path.
        _git(
            ["worktree", "add", "--force", "--detach", "--quiet", path, sha],
            cwd=live_root,
        )
    path = os.path.realpath(path)
    if os.path.exists(os.path.join(path, ".gitmodules")):
        _git(
            ["submodule", "update", "--init", "--recursive", "--force"],
            cwd=path, timeout=1800,
        )
    leftover = _dirty_records(path)
    if leftover:
        raise ReleaseCheckoutError(
            f"the release checkout at {path} is not clean after being reset "
            f"to {sha[:12]}:\n{_render(leftover)}\nDelete {path} and re-run; "
            f"the next release creates it afresh."
        )
    return path


def remove_checkout(live_root) -> str | None:
    """Remove the release checkout and its worktree registration.

    Returns the removed checkout's path, or None when the repository has no
    release checkout registered. The checkout's detached HEAD is a ref git
    counts as reachable, so it pins the history it was reset to; a history
    rewrite removes it first, and the next release creates it afresh. A
    registration whose directory is already gone is removed too. A directory at
    the checkout's path that git does not list as a worktree holds no ref and
    is left alone, as :func:`prepare_checkout` leaves it.
    """
    path = checkout_dir(live_root)
    listing = _out(["worktree", "list", "--porcelain"], cwd=live_root)
    registered = {
        os.path.realpath(line[len("worktree "):])
        for line in listing.splitlines() if line.startswith("worktree ")
    }
    if os.path.realpath(path) not in registered:
        return None
    # --force twice: a checkout with untracked or ignored files (the warm
    # caches) or a locked registration is removed all the same.
    _git(["worktree", "remove", "--force", "--force", path], cwd=live_root)
    return path


# ---------------------------------------------------------------------------
# advancing and unwinding the live branch
# ---------------------------------------------------------------------------


def _live_branch_tip(co):
    result = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{co.branch}^{{commit}}"],
        cwd=co.live_root, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _refuse_moved(co, expected, current, *, rerun):
    from .commands.release.execute import commit_subject

    lines = [
        f"Release aborted: {co.branch} moved while the release was running. "
        f"This release left it at {expected[:12]}; it is now at "
        f"{(current or 'nothing')[:12]}.",
    ]
    if current:
        appeared = _git(
            ["rev-list", f"{expected}..{current}"], cwd=co.live_root, check=False,
        ).stdout.split()
        if appeared:
            lines.append("")
            lines.append("Commits that appeared on the branch:")
            for sha in appeared:
                lines.append(f"  {sha[:12]}  {commit_subject(sha, cwd=co.live_root)}")
    lines.extend([
        "",
        f"Nothing was written to {co.branch} or to the working tree: the branch "
        f"only ever advances from where this release left it. To include those "
        f"commits, record them with `rlsbl changelog add` and {rerun}; to "
        f"exclude them, move them off {co.branch} first.",
    ])
    raise BranchMovedError("\n".join(lines))


_RESTORE_CHUNK = 500


def _write_back(repo_root, source, paths):
    """Make *paths* in the index and working tree at *repo_root* match *source*."""
    # GIT_LITERAL_PATHSPECS: a path is a path, never a glob. Chunked so a
    # release that regenerates many files stays far below the argv limit.
    for start in range(0, len(paths), _RESTORE_CHUNK):
        _git(
            ["restore", f"--source={source}", "--staged", "--worktree", "--",
             *paths[start:start + _RESTORE_CHUNK]],
            cwd=repo_root, env=_literal_env(),
        )


def advance_live_branch(new_sha, *, rerun, what="The release"):
    """Move the live release branch to *new_sha* and write its files back.

    A no-op outside a release checkout, and when the branch is already there.
    Refuses, writing nothing, when the branch is not where this release left
    it (:class:`BranchMovedError`), when the live working tree is not on the
    release branch, or when a path the advance writes has uncommitted changes
    in the live tree (:class:`LiveTreeConflictError`). *rerun* is what the
    refusal tells the operator to run once the cause is dealt with.
    """
    co = _active
    if co is None:
        return
    new = _out(["rev-parse", "--verify", f"{new_sha}^{{commit}}"], cwd=co.path)
    old = co.tip
    if new == old:
        return
    current = _live_branch_tip(co)
    if current != old:
        _refuse_moved(co, old, current, rerun=rerun)
    head = _git(["symbolic-ref", "--quiet", "HEAD"], cwd=co.live_root, check=False)
    if head.stdout.strip() != f"refs/heads/{co.branch}":
        raise ReleaseCheckoutError(
            f"the working tree at {co.live_root} is no longer on "
            f"{co.branch} (HEAD is "
            f"{head.stdout.strip() or 'detached'}), so the files of the "
            f"release's commits cannot be written into it. Nothing was written. "
            f"Check out {co.branch} again, then {rerun}."
        )
    paths = _changed_paths(old, new, cwd=co.path)
    blocking = [
        (status, path) for status, path in live_changes(co.live_root)
        if path in set(paths)
    ]
    if blocking:
        raise LiveTreeConflictError(
            conflict_message(blocking, what=what, rerun=rerun)
        )
    swap = _git(
        ["update-ref", "-m", f"rlsbl release: advance {co.branch}",
         f"refs/heads/{co.branch}", new, old],
        cwd=co.live_root, check=False,
    )
    if swap.returncode != 0:
        _refuse_moved(co, old, _live_branch_tip(co), rerun=rerun)
    _write_back(co.live_root, new, paths)
    co.tip = new
    co.last_advance = (old, new, paths)


def take_back(repo_root, branch, *, from_sha, to_sha):
    """Move *branch* from *from_sha* back to *to_sha*, by compare-and-swap.

    The inverse of an advance: the branch returns to *to_sha* only while it is
    still at *from_sha*, and the files that differ between the two are
    restored to *to_sha*'s content only while every one of them still holds
    *from_sha*'s (no uncommitted change in the working tree). Returns
    ``(True, None)`` when done and ``(False, reason)`` -- having written
    nothing -- when it would touch somebody else's work.
    """
    probe = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
        cwd=repo_root, check=False,
    )
    current = probe.stdout.strip() if probe.returncode == 0 else ""
    if current != from_sha:
        return False, (
            f"{branch} is at {(current or 'nothing')[:12]}, not at the "
            f"release commit {from_sha[:12]}: something was committed on top "
            f"of it"
        )
    paths = _changed_paths(to_sha, from_sha, cwd=repo_root)
    edited = [
        (status, path) for status, path in live_changes(repo_root)
        if path in set(paths)
    ]
    if edited:
        return False, (
            "files the release wrote were changed since:\n" + _render(edited)
        )
    swap = _git(
        ["update-ref", "-m", f"rlsbl release: take back {branch}",
         f"refs/heads/{branch}", to_sha, from_sha],
        cwd=repo_root, check=False,
    )
    if swap.returncode != 0:
        return False, f"{branch} moved while it was being taken back"
    _write_back(repo_root, to_sha, paths)
    return True, None


def unwind_last_advance():
    """Take back the most recent advance (see :func:`take_back`).

    Returns ``(True, None)`` when there was nothing to take back or it is
    taken back, and ``(False, reason)`` when doing so would touch somebody
    else's work, in which case nothing is written.
    """
    co = _active
    if co is None or co.last_advance is None:
        return True, None
    old, new, _paths = co.last_advance
    ok, reason = take_back(co.live_root, co.branch, from_sha=new, to_sha=old)
    if ok:
        co.tip = old
        co.last_advance = None
    return ok, reason


# ---------------------------------------------------------------------------
# entering the checkout
# ---------------------------------------------------------------------------


def write_scope(*, live_root, project_dirs, state_homes, extra=()):
    """The repository-relative paths a release writes, as far as it can be known.

    - every release state home (``.rlsbl/`` of the project, or the
      releasable's directory under ``.rlsbl-monorepo/releasables/``): the
      version marker, the changelog entries, the release file and its archive,
      and the config the release cleans;
    - each project's ``CHANGELOG.md`` and ``selfdoc.json``;
    - the version file of every target the projects release, and the
      lockfiles beside it that a version bump re-syncs;
    - *extra* (the workspace-level files a monorepo release writes).

    Generated files a producer writes (docs, schema dumps) cannot be named
    before the producer runs; they are covered when the release's commits
    advance the branch, which refuses the same way for any path it writes.
    """
    from .commands.release.execute import _LOCKFILE_SPECS
    from .targets import TARGETS, detect_targets

    def rel(p):
        r = os.path.relpath(os.path.realpath(p), live_root).replace(os.sep, "/")
        return "" if r == "." else r

    def join(base, name):
        return f"{base}/{name}" if base else name

    scope = set()
    for home in state_homes:
        r = rel(home)
        if r:
            scope.add(r)
    lock_names = {spec[0] for spec in _LOCKFILE_SPECS}
    for project_dir, releasable_dir in project_dirs:
        base = rel(project_dir)
        scope.add(join(base, "CHANGELOG.md"))
        scope.add(join(base, "selfdoc.json"))
        try:
            entries = detect_targets(project_dir, releasable_config_dir=releasable_dir)
        except Exception:
            entries = []
        for entry in entries:
            target = TARGETS.get(entry.name)
            tbase = rel(entry.path or project_dir)
            if target is not None:
                try:
                    vfile = target.version_file(entry.path or project_dir)
                except Exception:
                    vfile = None
                if vfile:
                    scope.add(join(tbase, vfile))
            for name in lock_names:
                scope.add(join(tbase, name))
    scope.update(e for e in extra if e)
    return sorted(scope)


def checkout_gowork(path) -> str:
    """The ``GOWORK`` every process of a release in the checkout at *path* gets.

    The checkout lives inside the live repository, and Go looks for a
    ``go.work`` in every parent directory, so without this a Go command in the
    checkout would find the live tree's uncommitted ``go.work`` and build
    against unreleased local modules (or fail to find the checkout's packages
    at all). A release builds committed state only: the checkout's own
    ``go.work`` when its root tracks one, and no workspace otherwise.
    """
    tracked = _git(
        ["ls-files", "--error-unmatch", "--", "go.work"],
        cwd=path, check=False, env=_literal_env(),
    )
    if tracked.returncode == 0:
        return os.path.join(path, "go.work")
    return "off"


def prepare_release_bin(live_root) -> str:
    """Create the release's own, empty directory for binaries; return its path.

    A hook builds into it (``$RLSBL_RELEASE_BIN``) a tool the release must run
    in its unreleased form -- selfdoc releasing itself runs the selfdoc it is
    about to ship -- without installing that build for every session on the
    machine. It sits beside the release checkout under the git common
    directory, outside the working tree and outside the checkout, and each
    release starts it empty.
    """
    path = os.path.join(_common_dir(live_root), RELEASE_BIN_RELATIVE)
    if os.path.lexists(path):
        effects.rmtree(path)
    effects.makedirs(path)
    return path


def release_environment(path, release_bin) -> dict[str, str]:
    """The environment variables every process of a release gets.

    *path* is the release checkout and *release_bin* the release's directory
    for binaries: ``GOWORK`` from :func:`checkout_gowork`, ``RLSBL_RELEASE_BIN``
    naming *release_bin*, and ``PATH`` with *release_bin* first, so a tool a
    hook built there is the one rlsbl's own steps and later hooks run.
    """
    return {
        "GOWORK": checkout_gowork(path),
        RELEASE_BIN_ENV: release_bin,
        "PATH": os.pathsep.join(
            p for p in (release_bin, os.environ.get("PATH")) if p
        ),
    }


@contextlib.contextmanager
def entered(live_root, *, branch, sha, cwd):
    """Enter the release checkout at *sha* for the duration of the block.

    Prepares the checkout and the release's directory for binaries, makes the
    checkout the active one, and moves the process into the checkout directory
    that corresponds to *cwd*, with :func:`release_environment` set for every
    process the release starts. On the way out the process returns to *cwd*,
    the environment is restored, and no checkout is active; the checkout itself
    is left as it is, to be reset by the next release.
    """
    global _active
    if _active is not None:
        raise ReleaseCheckoutError(
            "internal error: a release checkout is already active in this "
            "process; releases do not nest."
        )
    live_root = os.path.realpath(live_root)
    path = prepare_checkout(live_root, sha)
    overrides = release_environment(path, prepare_release_bin(live_root))
    co = ReleaseCheckout(live_root=live_root, path=path, branch=branch, tip=sha)
    here = os.getcwd()
    saved = {name: os.environ.get(name) for name in overrides}
    _active = co
    try:
        os.environ.update(overrides)
        os.chdir(co.to_checkout(cwd))
        yield co
    finally:
        _active = None
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        os.chdir(here)


def live_branch(live_root):
    """The branch the live working tree is on, refusing a detached HEAD."""
    head = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=live_root, check=False)
    branch = head.stdout.strip()
    if head.returncode != 0 or not branch:
        raise GitError(
            "HEAD is detached — a release runs from a named release branch. "
            "Check out the release branch and re-run."
        )
    return branch


def say(msg):
    """Print a release-checkout notice on stderr (``--quiet`` cannot hide it)."""
    print(msg, file=sys.stderr)
