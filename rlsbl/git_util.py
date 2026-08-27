"""Shared git utilities: commit ancestry, and commit-level file attribution.

Holds :func:`ancestry`, the single implementation of the "is A an ancestor of
B?" question every part of rlsbl asks (the mirror tripwire, the changelog
validation cache, the release candidate check, the resume check), plus the
commit-level half of file attribution -- retrieving the files a commit changed
and asking :mod:`rlsbl.ownership` who owns them -- and the SSH host check for
subtree remotes and manual-push detection.

Attribution itself (which member owns a path) is decided in
:mod:`rlsbl.ownership` and nowhere else.  This module only supplies the git
reads it needs.
"""

import enum
import re
import subprocess
import sys
from . import effects
from .ownership import (
    OwnershipError,
    member_name,
    owner_names_of_files,
)


class Ancestry(enum.Enum):
    """The outcome of an ancestry question, with "cannot tell" spelled out.

    ``git merge-base --is-ancestor`` answers with three exit codes, not two:
    0 means yes, 1 means no, and anything else (128, typically) means git could
    not answer -- an object the repository does not have, a broken object
    store.  Collapsing that third case into ``False`` makes "we do not know"
    indistinguishable from "we checked, and no", which is how a truncated
    history quietly turns into a wrong decision.

    Exit 1 is not always an honest "no" either, which is why :func:`ancestry`
    does more than translate exit codes.  In a SHALLOW repository the walk
    from the descendant stops at the graft boundary, so a commit whose
    connecting history was never downloaded gets exit 1 -- the same answer
    git gives for a genuinely unrelated commit, for a question it cannot
    answer.  Two ``--depth 1`` fetches into one repository reproduce it with
    both commits present.  :func:`ancestry` therefore reports
    :attr:`INDETERMINABLE` for exit 1 in a shallow repository, and keeps
    :attr:`FALSE` for exit 1 in a full one.

    Each caller decides what :attr:`INDETERMINABLE` means for it, and records
    the decision where the call is: the mirror reconciler and the release's
    recorded-candidate check branch on it separately from FALSE; the changelog
    validation cache and ``release resume`` fold it into the same fail-safe
    branch FALSE takes (recompute; refuse).
    """

    TRUE = "true"
    FALSE = "false"
    INDETERMINABLE = "indeterminable"


def ancestry(
    ancestor: str,
    descendant: str,
    cwd: str | None = None,
    *,
    timeout: int | None = 10,
) -> Ancestry:
    """Is *ancestor* reachable from *descendant*?  The one implementation.

    A commit is its own ancestor, as git has it.  Never raises: a timeout or a
    missing git binary is :attr:`Ancestry.INDETERMINABLE`, same as git's own
    "I cannot answer" exit code.

    Exact semantics, exit code by exit code:

    * **0** -> :attr:`Ancestry.TRUE`.
    * **1** -> :attr:`Ancestry.FALSE` in a full repository;
      :attr:`Ancestry.INDETERMINABLE` when the repository is shallow, because
      the walk stops at the graft boundary and git answers "no" to a question
      it could not follow to the end.
    * **anything else** (128, typically) -> :attr:`Ancestry.INDETERMINABLE`.

    The shallowness probe is itself a read that can fail (no repository at
    *cwd*, an ancient git).  An unreadable answer is taken as "not shallow",
    which keeps an ordinary "no" ordinary: a *cwd* that is no repository at
    all would have made ``merge-base`` exit 128 in the first place.

    Lives here rather than in a command handler because strictcli's
    effects-bypass lint reads a handler's own body: a ``ctx.effects`` call made
    through rlsbl's chokepoint module is indistinguishable, to that lint, from
    a raw subprocess call. Keeping effectful work out of the registration
    module is the shape the lint is asking for, and it is where this belongs
    anyway.
    """
    try:
        result = effects.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return Ancestry.INDETERMINABLE
    if result.returncode == 0:
        return Ancestry.TRUE
    if result.returncode == 1:
        if _is_shallow(cwd, timeout=timeout):
            return Ancestry.INDETERMINABLE
        return Ancestry.FALSE
    return Ancestry.INDETERMINABLE


def _is_shallow(cwd: str | None, *, timeout: int | None) -> bool:
    """Is the repository at *cwd* shallow?  False when the probe cannot say."""
    try:
        result = effects.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def get_commit_files(sha):
    """Get the list of files changed by a single commit.

    Returns a list of file paths relative to the repo root, or None on error.

    ``--root`` is required: a parentless commit (a repo's first commit) has
    nothing to diff against, and without it ``git diff-tree`` prints nothing
    at all -- making the commit that created the entire project look like it
    touched no files, so every project-scope match against it failed.
    """
    try:
        result = effects.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r",
             "-m", "--first-parent", "--root", sha],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


def commit_files(sha, *, operation):
    """Return the files a commit changed, or raise naming the commit.

    :func:`get_commit_files` answers ``None`` when git could not say -- a
    missing object, a timeout, no git at all.  Every attribution caller used to
    turn that into a guess (include the commit "to be safe", or drop it), so a
    broken read silently changed which member a commit was charged to.  It is
    a hard error instead, naming the commit and the operation that asked.
    """
    files = get_commit_files(sha)
    if files is None:
        raise OwnershipError(
            f"cannot determine the files changed by commit {sha} "
            f"({operation}): `git diff-tree` gave no answer. The commit may be "
            f"missing from this repository (a shallow clone, a rewritten "
            f"history), or git is unavailable. Fetch the commit or run the "
            f"command in a full clone -- attribution cannot be guessed."
        )
    return files


def commit_owner_names(sha, members, *, operation) -> set:
    """Names of the members owning any file the commit *sha* changed.

    Tool-owned paths (:mod:`rlsbl.ownership`) contribute no owner, so a commit
    that only touches changelog state yields the empty set.
    """
    return owner_names_of_files(commit_files(sha, operation=operation), members)


def filter_commits_for_scope(commits, scope, *, operation):
    """Filter *commits* to those touching a file owned by a member in *scope*.

    *scope* is an :class:`~rlsbl.ownership.OwnershipScope`, which carries the
    whole member list alongside the subset asked about -- attribution needs
    both, because a file's owner is decided against every member, not just the
    ones the caller cares about.  ``None`` means "no workspace" and returns
    *commits* unchanged.
    """
    if scope is None:
        return set(commits)
    filtered = set()
    for sha in commits:
        if scope.claims_any(commit_files(sha, operation=operation)):
            filtered.add(sha)
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
        result = effects.run(
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


def get_push_changed_files(refs):
    """Get the list of files changed across all pushed refs.

    Takes a list of (local_sha, remote_sha) tuples (as returned by
    ``_parse_stdin_refs`` in prepush_utils.py).

    Returns a set of file paths relative to the repo root, or None if
    git commands fail.
    """
    zero_sha = "0" * 40
    changed = set()

    for local_sha, remote_sha in refs:
        if local_sha == zero_sha:
            # Branch being deleted -- nothing to check
            continue

        try:
            if remote_sha == zero_sha:
                # New branch: get files in commits not yet on any remote
                result = effects.run(
                    ["git", "log", "--name-only", "--pretty=format:", local_sha,
                     "--not", "--remotes"],
                    capture_output=True, text=True, timeout=30,
                )
            else:
                result = effects.run(
                    ["git", "--no-optional-locks", "diff", "--name-only", f"{remote_sha}..{local_sha}"],
                    capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0:
                for f in result.stdout.strip().splitlines():
                    f = f.strip()
                    if f:
                        changed.add(f)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    return changed


def affected_members(changed_files, members):
    """Determine which workspace members own at least one of *changed_files*.

    Single-owner attribution: a file counts for exactly one member, so a change
    under ``pkg/inner`` affects ``pkg/inner`` and not its parent, and a change
    to a root file affects the root member alone.  Members are returned in
    workspace declaration order.
    """
    owners = owner_names_of_files(changed_files, members)
    return [m for m in members if member_name(m) in owners]


def detect_manual_push_branches(stdin_lines, release_branches):
    """Return list of release branch names being pushed to manually.

    Parses pre-push hook stdin lines for ``refs/heads/<branch>`` patterns
    and returns branch names that match *release_branches*.

    Returns an empty list if no release branches are being pushed to or if
    *stdin_lines* is empty/None.

    There is deliberately NO environment-variable bypass. Release-internal
    pushes run ``git push --no-verify``, so they never invoke the hook at all;
    every push that reaches this function is a hook-running push, i.e. manual.
    """
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
