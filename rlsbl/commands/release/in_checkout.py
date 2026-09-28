"""The release commands' way into the release checkout.

``rlsbl release run``, ``rlsbl release resume`` and ``rlsbl monorepo release
run`` all start here. :func:`run_in_release_checkout` looks at the operator's
working tree once, decides which of its uncommitted changes would block the
release (the ones inside the paths the release writes) and which it leaves
alone (everything else), takes the release lock, and runs the release in the
release checkout of the branch tip (see :mod:`rlsbl.release_checkout`).

A dry run takes none of those steps: it reports the same two lists without
refusing, and previews the working tree as it stands.
"""

import os
from pathlib import Path

from ... import release_checkout
from ...context import create_context
from ...lock import acquire_lock, release_lock


def project_scope(project_root, workspace_root):
    """The write scope of one release: ``(project_dirs, state_homes, extra)``.

    A standalone project writes its ``.rlsbl/`` state; a releasable writes its
    state directory under ``.rlsbl-monorepo/releasables/``, its members'
    version files, the workspace changelog, and the monorepo snapshot.
    """
    from .release_state import resolve_releasable_dir

    project_root = str(project_root)
    if workspace_root is None:
        return (
            [(project_root, None)],
            [os.path.join(project_root, ".rlsbl")],
            (),
        )
    workspace_root = str(workspace_root)
    releasable_dir = resolve_releasable_dir(project_root, workspace_root)
    members = [(project_root, releasable_dir)]
    if releasable_dir is not None:
        from ...workspace import load_workspace, members_of

        name = os.path.basename(os.path.normpath(releasable_dir))
        members = [
            (os.path.join(workspace_root, p["path"]), releasable_dir)
            for p in members_of(name, load_workspace(workspace_root))
        ]
    homes = [releasable_dir or os.path.join(project_root, ".rlsbl")]
    extra = (
        ".rlsbl-monorepo/snapshot.json",
        _rel(workspace_root, os.path.join(workspace_root, "CHANGELOG.md")),
    )
    return members, homes, extra


def workspace_scope(workspace_root):
    """The write scope of a batch release: the whole workspace's release state.

    A batch releases the releasables its release file names, and rewrites the
    workspace's own release state (the batch file and its plan, the snapshot,
    the combined changelog, the root's generated docs) on the way, so every
    member's version files and all of ``.rlsbl-monorepo/`` are in it.
    """
    from ...workspace import get_releasable_dir, load_workspace

    workspace_root = str(workspace_root)
    members = []
    for p in load_workspace(workspace_root):
        rel_name = p.get("releasable") if hasattr(p, "get") else None
        rel_dir = (
            get_releasable_dir(workspace_root, rel_name)
            if isinstance(rel_name, str) and rel_name else None
        )
        members.append((os.path.join(workspace_root, p["path"]), rel_dir))
    homes = [os.path.join(workspace_root, ".rlsbl-monorepo")]
    extra = (
        _rel(workspace_root, os.path.join(workspace_root, "CHANGELOG.md")),
        _rel(workspace_root, os.path.join(workspace_root, "selfdoc.json")),
    )
    return members, homes, extra


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _git_root(path):
    from ...utils import run

    return os.path.realpath(run("git", ["rev-parse", "--show-toplevel"], cwd=str(path)))


def run_in_release_checkout(ctx, flags, body, *, scope, rerun, lock_dir,
                            lock_root, log):
    """Run ``body(ctx, flags)`` for a release, in the release checkout.

    *scope* is ``(project_dirs, state_homes, extra)`` (see
    :func:`project_scope` and :func:`workspace_scope`); an uncommitted change
    inside it refuses the release before anything is written, naming every
    such path, and every other uncommitted change is listed and left alone.
    *rerun* is the command the refusal tells the operator to run once those
    changes are committed.

    Under ``--dry-run`` nothing refuses: the report is printed, and *body*
    previews the working tree in place.
    """
    # Absolute before the process moves into the checkout, where a relative
    # path would resolve against the checkout instead of the working tree.
    project_root = os.path.abspath(str(ctx.project_root))
    workspace_root = (
        os.path.abspath(str(ctx.workspace_root)) if ctx.workspace_root else None
    )
    live_root = _git_root(workspace_root or project_root)
    project_dirs, state_homes, extra = scope
    write_scope = release_checkout.write_scope(
        live_root=live_root, project_dirs=project_dirs,
        state_homes=state_homes, extra=extra,
    )
    dry_run = bool(flags.get("dry-run"))

    if dry_run:
        blocking, ignored = release_checkout.partition_changes(
            release_checkout.live_changes(live_root), write_scope,
        )
        if blocking:
            release_checkout.say(
                "Dry run: the release would refuse to start, because it "
                "writes these paths and they have uncommitted changes:\n"
                + "\n".join(f"  {s} {p}" for s, p in blocking)
            )
        release_checkout.report_ignored(ignored, log=release_checkout.say)
        return body(ctx, flags)

    acquire_lock(lock_dir=lock_dir, project_root=lock_root)
    try:
        blocking, ignored = release_checkout.partition_changes(
            release_checkout.live_changes(live_root), write_scope,
        )
        if blocking:
            from .validate import ReleaseValidationError

            raise ReleaseValidationError(release_checkout.conflict_message(
                blocking, what="The release", rerun=rerun,
            ))
        release_checkout.report_ignored(ignored, log=release_checkout.say)
        branch = release_checkout.live_branch(live_root)
        sha = release_checkout._out(
            ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
            cwd=live_root,
        )
        cwd = os.getcwd()
        if os.path.realpath(cwd) != live_root and not os.path.realpath(
            cwd,
        ).startswith(live_root + os.sep):
            cwd = project_root
        with release_checkout.entered(
            live_root, branch=branch, sha=sha, cwd=cwd,
        ) as co:
            log(f"Releasing from the release checkout at {co.path} ({sha[:12]})")
            workspace = (
                Path(co.to_checkout(workspace_root)) if workspace_root else None
            )
            checkout_ctx = create_context(
                Path(co.to_checkout(project_root)), workspace_root=workspace,
            )
            inner = dict(flags)
            inner["skip-lock"] = True
            return body(checkout_ctx, inner)
    finally:
        release_lock()
