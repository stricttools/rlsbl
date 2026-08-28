"""Where the one ``uv.lock`` that resolves a given manifest lives.

There is exactly ONE lock that resolves a manifest.  Usually it sits beside it.
In a uv WORKSPACE it does not: members have no lock of their own, and one lock
at the workspace root resolves every member.  Anything that reads a locked
version therefore has to resolve the lock's LOCATION first:

1. ``<project>/uv.lock`` when it exists;
2. otherwise the ``uv.lock`` of the nearest ancestor that declares
   ``[tool.uv.workspace]`` and whose ``members`` / ``exclude`` globs claim the
   project directory (:func:`find_uv_workspace_root`);
3. otherwise nothing -- and the search says which two locations it probed, so
   the caller can name them.

This is location resolution for ONE authoritative answer, not a try-A-then-B
strategy with two different answers: a workspace member has no second lock that
could disagree.  When both files do exist -- a member carrying a stale lock of
its own -- the one beside the manifest wins outright, because that is the lock
uv itself would use for a standalone project.

Readability is deliberately NOT decided here.  A lock that exists but does not
parse is still the location; refusing to read it is the reader's job, and a
locator that walked past an unreadable lock would silently answer from a
different file.

Three callers share this: ``rlsbl rewrite uv-path-sources`` (which built it),
the ``dep-floors`` check, and the ``dep-locks`` check.  Before it was shared,
``dep-floors`` looked only beside the project root, so every member of a uv
workspace reported "no uv.lock" and its declared floors went unpoliced.
"""

import glob
import os
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class LockLocation:
    """The one lock that resolves a manifest, and how it was reached."""

    path: str
    #: The uv workspace root whose lock this is, or None when the lock sits
    #: beside the manifest.
    workspace_root: str | None = None

    def label(self, project_root):
        """How the lock is named in errors -- relative to the target."""
        return os.path.relpath(self.path, str(project_root))

    def describe(self, project_root):
        """One fact line naming which lock a version came from."""
        rel = self.label(project_root)
        if self.workspace_root is None:
            return f"floor read from {rel} (beside the manifest)"
        return (
            f"floor read from {rel} -- the uv workspace root at "
            f"{os.path.relpath(self.workspace_root, str(project_root))} "
            f"claims this directory as a member, and a member has no lock "
            f"of its own"
        )


@dataclass(frozen=True)
class LockSearch:
    """The outcome of looking for a manifest's lock.

    *location* is None when no lock was found; *probed* then describes every
    location that was looked at, so a caller's refusal can name them.
    """

    location: LockLocation | None
    probed: str


def uv_workspace_table(pyproject_path):
    """``[tool.uv.workspace]`` of *pyproject_path*, or None when it has none."""
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = (((data or {}).get("tool") or {}).get("uv") or {}).get("workspace")
    return table if isinstance(table, dict) else None


def _glob_claims(patterns, workspace_root, target):
    """True when any glob in *patterns* expands to *target*.

    Expanded against the real filesystem, which is how uv reads them: the
    globs are relative to the workspace root, ``*`` stops at a directory
    separator and ``**`` crosses them.
    """
    for pattern in patterns or ():
        if not isinstance(pattern, str):
            continue
        for hit in glob.glob(pattern, root_dir=workspace_root, recursive=True):
            if os.path.realpath(os.path.join(workspace_root, hit)) == target:
                return True
    return False


def workspace_claims(workspace_root, table, target):
    """uv's membership rule: a ``members`` glob hits, no ``exclude`` glob does."""
    members = table.get("members")
    if not isinstance(members, list):
        return False
    if not _glob_claims(members, workspace_root, target):
        return False
    exclude = table.get("exclude")
    if isinstance(exclude, list) and _glob_claims(exclude, workspace_root, target):
        return False
    return True


def find_uv_workspace_root(project_root):
    """The uv workspace root that claims *project_root*, or None.

    uv's own discovery, walked the same way: the FIRST ancestor declaring
    ``[tool.uv.workspace]`` decides.  A directory that declaration does not
    claim -- no ``members`` glob matches it, or an ``exclude`` glob does -- is
    a standalone project, not a member of some further ancestor; uv forbids
    nested workspaces, so there is never a second declaration to consult.
    """
    target = os.path.realpath(str(project_root))
    current = os.path.dirname(target)
    while True:
        table = uv_workspace_table(os.path.join(current, "pyproject.toml"))
        if table is not None:
            return current if workspace_claims(current, table, target) else None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def locate_uv_lock(project_root):
    """Return the :class:`LockSearch` for the manifest in *project_root*.

    Existence only: a lock that is present but unparseable is still the
    location (see the module docstring).
    """
    root = str(project_root)
    beside = os.path.join(root, "uv.lock")
    if os.path.isfile(beside):
        return LockSearch(
            location=LockLocation(path=beside), probed=f"{beside} (present)",
        )

    workspace_root = find_uv_workspace_root(root)
    if workspace_root is not None:
        workspace_lock = os.path.join(workspace_root, "uv.lock")
        if os.path.isfile(workspace_lock):
            return LockSearch(
                location=LockLocation(
                    path=workspace_lock, workspace_root=workspace_root,
                ),
                probed=f"{workspace_lock} (present)",
            )
        return LockSearch(
            location=None,
            probed=(
                f"{beside} (absent) and {workspace_lock} (absent), the lock of "
                f"the uv workspace root that claims this directory as a member"
            ),
        )

    return LockSearch(
        location=None,
        probed=(
            f"{beside} (absent); no ancestor declares a [tool.uv.workspace] "
            f"claiming this directory, so there is no workspace lock to read "
            f"either"
        ),
    )
