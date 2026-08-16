"""Rename a releasable group: ``rlsbl monorepo rename-releasable <old> <new>``.

Renaming a releasable is a coordinated, idempotent operation:

1. Rewrite ``workspace.toml`` in place -- the ``[[releasables]]`` table's
   ``name`` field and every member project's ``releasable`` field -- preserving
   comments and key order.
2. Move the releasable's state directory
   (``.rlsbl-monorepo/releasables/<old>`` -> ``<new>``).
3. Delete the moved ``changes/.validated`` cache (the tag glob changes, so the
   validation cache is stale).
4. Re-run ``monorepo sync`` to regenerate the publish gate prefixes and CI
   router.
5. Commit all of the above as a single commit.

Then, last, when the releasable's ``tag_format`` contains ``{name}`` (so the
tag prefix actually changes), a boundary alias tag for the current version is
created at the commit the old current-version tag points to and pushed. This
is the single sanctioned remote action. Historical releases stay under the old
prefix and are no longer managed by ``rlsbl release edit/deprecate/yank``.

The flow is idempotent: a crash between the local commit and the tag push is
healed by re-running the command, which detects the already-renamed state and
finishes the tag step.
"""

import os
import re
import subprocess

import tomlkit
from tomlkit.items import AoT

from ...errors import WorkspaceError
from ...release_file import get_batch_release_file_path
from ...config import read_project_config
from ...utils import (
    check_gh_auth,
    check_gh_installed,
    commit_files_if_changed,
    get_push_timeout,
    run,
)
from ...workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    get_releasable_changes_dir,
    get_releasable_dir,
    is_explicit_mode,
    load_releasables,
    load_workspace,
    members_of,
    read_releasable_version,
)
from ... import effects
from ...saferm import saferm_delete


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def validate_releasable_name(name):
    """Validate a releasable name against the ``[a-z][a-z0-9-]*`` charset.

    Raises WorkspaceError when the name is not a string, is empty, or contains
    characters outside the allowed set (lowercase letters, digits, hyphens,
    starting with a letter).
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise WorkspaceError(
            f"invalid releasable name '{name}': must match [a-z][a-z0-9-]* "
            "(lowercase letter first, then lowercase letters, digits, or hyphens)"
        )


# ---------------------------------------------------------------------------
# git helpers (all local, cwd=workspace_root)
# ---------------------------------------------------------------------------


def _is_clean_tree(root):
    """Return True when the git working tree at ``root`` is clean."""
    status = run("git", ["--no-optional-locks", "status", "--porcelain"], cwd=root)
    return len(status.strip()) == 0


def _tag_exists_local(root, tag):
    """Return True when ``tag`` exists as a local git tag."""
    try:
        run("git", ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=root)
        return True
    except subprocess.CalledProcessError:
        return False


def _tag_exists_remote(root, remote, tag):
    """Return True when ``tag`` exists on ``remote``."""
    try:
        out = run("git", ["ls-remote", "--tags", remote, tag], cwd=root)
    except subprocess.CalledProcessError:
        return False
    return bool(out.strip())


def _resolve_tag_commit(root, tag):
    """Resolve ``tag`` to the commit SHA it points to, or None if absent."""
    try:
        sha = run("git", ["rev-parse", f"{tag}^{{commit}}"], cwd=root)
    except subprocess.CalledProcessError:
        return None
    sha = sha.strip()
    return sha if len(sha) == 40 else None


def _saferm_file(path):
    """Delete a stale cache file via saferm (audit trail; -f skips if missing)."""
    saferm_delete(
        path,
        skip_missing=True,
        description=(
            "Removing stale changelog validation cache after releasable rename"
        ),
    )


# ---------------------------------------------------------------------------
# workspace.toml in-place rename (reuses tomlkit in-place editing, like 5.1)
# ---------------------------------------------------------------------------


def _apply_workspace_rename(root, old, new):
    """Rewrite workspace.toml in place: releasable name + member fields.

    Locates the ``[[releasables]]`` table whose ``name`` equals ``old`` and
    rewrites only its ``name`` field, then rewrites every ``[[projects]]``
    table whose ``releasable`` equals ``old`` to ``new``. All other content --
    comments, key order, unrelated tables -- is preserved byte-for-byte.

    Returns True when the file was changed, False when it was already renamed
    (idempotent no-op).
    """
    path = os.path.join(root, WORKSPACE_DIR, WORKSPACE_FILE)
    with open(path, encoding="utf-8") as f:
        doc = tomlkit.loads(f.read())

    changed = False

    rels = doc.get("releasables")
    if isinstance(rels, AoT):
        for table in rels:
            if table.get("name") == old:
                table["name"] = new
                changed = True

    projs = doc.get("projects")
    if isinstance(projs, AoT):
        for table in projs:
            if table.get("releasable") == old:
                table["releasable"] = new
                changed = True

    if changed:
        effects.atomic_write_text(path, tomlkit.dumps(doc))

    return changed


# ---------------------------------------------------------------------------
# local mutations (steps 1-5) and the alias-tag step (last)
# ---------------------------------------------------------------------------


def _apply_local_rename(root, old, new):
    """Perform the local mutations (steps 1-5) and commit them as one commit.

    Structured as a standalone callable so tests can invoke it (simulating a
    crash right after the commit, before the tag push) and then run the full
    command to verify the tag step is healed on re-run.
    """
    # Step 1: in-place workspace.toml edit.
    _apply_workspace_rename(root, old, new)

    # Step 2: move the releasable state directory (git sees a rename).
    old_dir = get_releasable_dir(root, old)
    new_dir = get_releasable_dir(root, new)
    if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
        effects.rename(old_dir, new_dir)

    # Step 3: drop the moved changes/.validated cache (stale after prefix change).
    validated = os.path.join(get_releasable_changes_dir(root, new), ".validated")
    if os.path.exists(validated):
        _saferm_file(validated)

    # Step 3b: invalidate the publish-router cache. Its hash keys on project
    # *names* and publish.yml *content*, neither of which changes on a
    # releasable rename -- but the gate prefix (derived from the releasable's
    # tag_format) does. Dropping the cache forces sync to regenerate the
    # router with the new prefix.
    from .publish_inline import PUBLISH_CACHE_FILENAME
    publish_cache = os.path.join(root, WORKSPACE_DIR, PUBLISH_CACHE_FILENAME)
    if os.path.exists(publish_cache):
        _saferm_file(publish_cache)

    # Step 4: re-run sync (no auto-commit -- we commit everything once below).
    from .sync import _cmd_sync
    _cmd_sync({"auto-commit": False}, project_root=root)

    # Step 5: one commit of everything (workspace.toml, moved dir, workflows,
    # publish cache, .validated deletion). safegit stages the rename and the
    # deletion under these paths. ``commit_files_if_changed`` makes this a no-op
    # when the rename is already committed, so re-running the full tail on
    # resume never produces a spurious/empty commit.
    commit_files_if_changed(
        f"monorepo: rename releasable {old} -> {new}",
        [WORKSPACE_DIR, os.path.join(".github", "workflows")],
        skip_message="rename already committed; nothing to commit.",
        cwd=root,
    )


def _push_timeout_for(root, name):
    """Resolve the push timeout from the renamed releasable's config.

    The workspace root has no per-package ``.rlsbl/config.json`` in a monorepo,
    so this reads the releasable-level config and lets the standard
    ``push_timeout`` key (or its default) apply.
    """
    return get_push_timeout(read_project_config(root, get_releasable_dir(root, name)))


def _finish_alias_tag(root, old_tag, new_tag, remote, *, push_timeout):
    """Create the boundary alias tag and push it, idempotently.

    Returns a status dict describing what was (or would have been) done.
    """
    local = _tag_exists_local(root, new_tag)
    remote_has = _tag_exists_remote(root, remote, new_tag)

    if local and remote_has:
        return {"status": "already_done", "tag": new_tag}

    if not local:
        commit = _resolve_tag_commit(root, old_tag)
        if commit is None:
            # No current-version tag to alias -- releasable was never released
            # at this version. Nothing to carry forward.
            return {"status": "no_source_tag", "old_tag": old_tag, "tag": new_tag}
        run("git", ["tag", new_tag, commit], cwd=root)

    # Push ONLY the alias tag -- the single sanctioned remote action.
    # --no-verify: the pre-push hook is a changelog-coverage guard for branch
    # pushes; a tool-driven tag push must not be gated by it.
    run("git", ["push", "--no-verify", remote, new_tag], cwd=root,
        timeout=push_timeout)
    return {"status": "created", "tag": new_tag}


def _unmanaged_history_note(old_prefix, new_prefix):
    """Return the note printed after a prefix-changing rename."""
    return (
        f"Note: historical releases remain tagged under the old prefix "
        f"'{old_prefix}'. They are no longer managed by rlsbl release "
        f"edit/deprecate/yank, which now operate on the new prefix "
        f"'{new_prefix}'. Only the current version was aliased forward."
    )


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _check_no_inflight(root, releasables):
    """Hard-error when a release is in flight (workspace or member state)."""
    batch_file = get_batch_release_file_path(root)
    if os.path.isfile(batch_file):
        raise WorkspaceError(
            f"a workspace release file is in flight: {batch_file}. "
            "Finish or remove the release before renaming."
        )
    for rel in releasables:
        in_progress = os.path.join(
            get_releasable_dir(root, rel.name), "releases", "in-progress.json"
        )
        if os.path.isfile(in_progress):
            raise WorkspaceError(
                f"releasable '{rel.name}' has a release in progress: "
                f"{in_progress}. Resume or abort it before renaming."
            )


def _announce_rename(old, new, alias_tag):
    """Print what the rename is about to do, before any mutation.

    An announcement, not a gate.  `monorepo rename-releasable` is not
    `consequential`: the local half is an ordinary commit, and the only thing
    that leaves the repo is one ADDITIVE alias tag that can be deleted.  The
    flow is idempotent, so a re-run heals a partial rename.
    """
    lines = [
        f"\nRenaming releasable '{old}' -> '{new}':",
        "  - rewrite workspace.toml and move the releasable state directory",
        "  - regenerate the publish gate prefix and commit the rename",
    ]
    if alias_tag:
        lines.append(
            f"  - create and PUSH the alias tag '{alias_tag}' to the remote"
        )
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def rename_releasable(workspace_root, old_name, new_name, *, dry_run=False,
                      remote="origin"):
    """Rename releasable ``old_name`` to ``new_name`` in a monorepo workspace.

    See the module docstring for the full ordered flow. Returns a result dict
    describing what was done (or, in dry-run, what would be done).

    Raises WorkspaceError on any preflight failure.
    """
    root = str(workspace_root)

    if not is_explicit_mode(root):
        raise WorkspaceError(
            "rename-releasable requires explicit mode "
            "([[releasables]] in workspace.toml)."
        )

    projects = load_workspace(root)
    releasables = load_releasables(root, projects)
    names = {r.name for r in releasables}
    project_names = {p.name for p in projects}

    old_dir = get_releasable_dir(root, old_name)
    new_dir = get_releasable_dir(root, new_name)

    old_present = old_name in names
    new_present = new_name in names
    dir_moved = os.path.isdir(new_dir) and not os.path.isdir(old_dir)

    # ---- resume path: the local rename was applied but may not have been fully
    #      committed (a crash anywhere before the tag push). Run the FULL
    #      idempotent tail -- ``_apply_local_rename`` (workspace rewrite no-ops
    #      when done, dir move is guarded, saferm -f, sync is deterministic,
    #      commit only when dirty) then ``_finish_alias_tag`` -- so any crash
    #      window heals completely: the pending rename + regenerated gate get
    #      committed BEFORE the alias tag is ever pushed. ----
    if new_present and not old_present and dir_moved:
        target_rel = next(r for r in releasables if r.name == new_name)
        tag_format = target_rel.tag_format
        version = read_releasable_version(root, new_name)
        name_in_format = "{name}" in tag_format
        old_tag = tag_format.format(name=old_name, version=version)
        new_tag = tag_format.format(name=new_name, version=version)
        result = {
            "mode": "resume",
            "old": old_name,
            "new": new_name,
            "tag_format": tag_format,
            "version": version,
            "tag": None,
        }
        if name_in_format:
            result["note"] = _unmanaged_history_note(
                tag_format.format(name=old_name, version=""),
                tag_format.format(name=new_name, version=""),
            )

        if dry_run:
            if name_in_format:
                result["planned_tag"] = new_tag
                result["planned_push"] = f"git push {remote} {new_tag}"
            return result

        if name_in_format and not (check_gh_installed() and check_gh_auth()):
            raise WorkspaceError(
                "gh CLI is not installed or not authenticated "
                "(run 'gh auth login')."
            )

        _announce_rename(
            old_name, new_name, new_tag if name_in_format else None,
        )

        _apply_local_rename(root, old_name, new_name)
        if name_in_format:
            result["tag"] = _finish_alias_tag(
                root, old_tag, new_tag, remote,
                push_timeout=_push_timeout_for(root, new_name),
            )
        else:
            result["name_only"] = True
        return result

    # ---- fresh-run preflight (all hard errors) ----
    validate_releasable_name(new_name)

    if old_name == new_name:
        raise WorkspaceError("old and new releasable names are identical.")
    if not old_present:
        raise WorkspaceError(
            f"releasable '{old_name}' not found "
            f"(available: {sorted(names)})."
        )
    if new_present:
        raise WorkspaceError(
            f"releasable '{new_name}' already exists."
        )
    if new_name in project_names:
        raise WorkspaceError(
            f"'{new_name}' collides with an existing project name."
        )
    if os.path.isdir(new_dir):
        raise WorkspaceError(
            f"target releasable directory already exists: {new_dir}."
        )
    if not _is_clean_tree(root):
        raise WorkspaceError(
            "working tree is not clean. Commit or set aside changes first."
        )
    _check_no_inflight(root, releasables)

    target_rel = next(r for r in releasables if r.name == old_name)
    tag_format = target_rel.tag_format
    version = read_releasable_version(root, old_name)
    name_in_format = "{name}" in tag_format
    members = members_of(old_name, projects)
    member_names = [m.name for m in members]

    old_tag = tag_format.format(name=old_name, version=version)
    new_tag = tag_format.format(name=new_name, version=version)

    # gh auth is only required when an alias tag will actually be created and
    # pushed (tag_format contains {name}); a name-only rename touches no remote.
    # Kept last among the preflights so it does not mask the other guards.
    if not dry_run and name_in_format and not (check_gh_installed() and check_gh_auth()):
        raise WorkspaceError(
            "gh CLI is not installed or not authenticated (run 'gh auth login')."
        )

    result = {
        "mode": "rename",
        "old": old_name,
        "new": new_name,
        "tag_format": tag_format,
        "version": version,
        "members": member_names,
        "name_in_format": name_in_format,
        "tag": None,
    }

    # ---- dry-run: full plan, zero mutations ----
    if dry_run:
        plan = [
            f"1. edit workspace.toml: releasable '{old_name}' -> '{new_name}' "
            f"and {len(member_names)} member(s) ({', '.join(member_names)})",
            f"2. move directory {get_releasable_dir(root, old_name)} -> "
            f"{new_dir}",
            "3. delete moved changes/.validated cache (saferm)",
            "4. re-run monorepo sync (regenerate publish gate prefixes/router)",
            f"5. commit: 'monorepo: rename releasable {old_name} -> {new_name}'",
        ]
        if name_in_format:
            plan.append(
                f"6. create alias tag '{new_tag}' at the commit of '{old_tag}'"
            )
            plan.append(f"7. git push {remote} {new_tag}")
            result["note"] = _unmanaged_history_note(
                tag_format.format(name=old_name, version=""),
                tag_format.format(name=new_name, version=""),
            )
        else:
            plan.append(
                "6. name-only rename (tag_format has no {name}); "
                "no alias tag, no push"
            )
        result["plan"] = plan
        result["planned_tag"] = new_tag if name_in_format else None
        result["planned_push"] = (
            f"git push {remote} {new_tag}" if name_in_format else None
        )
        return result

    # ---- announcement (before ANY mutation) ----
    _announce_rename(
        old_name, new_name, new_tag if name_in_format else None,
    )

    # ---- mutations (steps 1-5) ----
    _apply_local_rename(root, old_name, new_name)

    # ---- alias tag + push (last) ----
    if name_in_format:
        tag_result = _finish_alias_tag(
            root, old_tag, new_tag, remote,
            push_timeout=_push_timeout_for(root, new_name),
        )
        result["tag"] = tag_result
        result["note"] = _unmanaged_history_note(
            tag_format.format(name=old_name, version=""),
            tag_format.format(name=new_name, version=""),
        )
    else:
        result["name_only"] = True

    return result
