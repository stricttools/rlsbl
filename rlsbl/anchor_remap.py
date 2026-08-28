"""Moving the release ledger's anchors through a history rewrite's commit map.

A release archive records the commit a version shipped from -- its
``candidate_sha`` -- and the git tree each released path carried. Both name
objects in the commit graph, and a history rewrite replaces that graph. The
JSONL changelog has been remapped through the rewrite's old-to-new commit map
since scrubbing existed; the archives were not, so after a scrub the ledger's
anchor and the (correctly moved) tag disagreed, and
:func:`rlsbl.ledger.read_entry` refused every read with the DISAGREEMENT error
-- an error that blames the tag for moving, which is precisely backwards.

The repair is this module, and it is deliberately narrow:

* an anchor whose commit the map names is rewritten to the new commit;
* an anchor the map does not mention is left exactly as it is;
* an ``unanchorable`` archive has no commit to move and is skipped.

**The content check, and the one caller that cannot pass it.** Each recorded
tree hash is recomputed at the new commit and compared. A rewrite that only
re-parents commits leaves every tree byte-identical, and the version really did
ship the same content from a differently-named commit. A rewrite that REDACTED
something under a released path did not, and re-anchoring the archive to it
would make the archive claim content the release never shipped -- so by default
a mismatch is a hard error naming the version and both hashes, and nothing is
written.

``rlsbl release scrub`` is the caller that must state otherwise, and the reason
is its own doing: it passes ``--remap-shas-in`` so safegit rewrites the JSONL
changelog files AT EVERY COMMIT of the rewritten history. Those files are
INSIDE the released tree, and they name commits the rewrite moved, so a scrub
of an rlsbl-managed repository changes every released tree by construction --
the default check would refuse every scrub that repository ever runs. That
caller passes ``on_content_change="record"``, which recomputes and records the
new trees and PRINTS every path whose tree changed, so the change is visible in
the scrub's output rather than absorbed. Which behavior applies is the caller's
declared choice, never something decided from what is observed.

Every archive is validated before ANY of them is written, so a failure on one
version leaves the whole ledger untouched rather than half-remapped.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from . import effects
from .errors import RlsblError
from .release_file import (
    archived_release_path,
    list_archived_versions,
    read_release_file,
    writable_release_file,
    write_release_anchor,
)

# One git object read. Generous enough for a cold repository, bounded because
# every external call in rlsbl states its timeout.
_READ_TIMEOUT = 30


# What a caller does when a released path's tree is not the same object at the
# rewritten commit. There is no third value and no default derived from what is
# observed: each caller declares which of the two it means.
ON_CONTENT_CHANGE_REFUSE = "refuse"
ON_CONTENT_CHANGE_RECORD = "record"
ON_CONTENT_CHANGE = (ON_CONTENT_CHANGE_REFUSE, ON_CONTENT_CHANGE_RECORD)


@dataclass(frozen=True)
class AnchorRemap:
    """One archive's anchor, moved.

    ``tree_hashes`` is what the rewritten archive will record, and
    ``changed_paths`` names the released paths whose tree is a different object
    at the new commit -- empty whenever the content was proved identical.
    """

    version: str
    path: str
    old_sha: str
    new_sha: str
    tree_hashes: dict
    changed_paths: tuple = ()


def _map_sha(sha, commit_map):
    """The rewritten commit for *sha*, or None when the map does not name it.

    Abbreviation-tolerant in the same way the changelog remap is: a stored
    anchor may be shorter than 40 characters, and it maps when it prefixes
    exactly one key.
    """
    from .changelog.files import _map_hash

    new_sha, ambiguous = _map_hash(sha, commit_map)
    if ambiguous:
        raise RlsblError(
            f"the release anchor {sha} matches more than one commit in the "
            f"rewrite map, so which commit it moved to cannot be decided. "
            f"Record the full 40-character sha in the archive and re-run."
        )
    return new_sha


def _tree_at(sha, path, *, cwd):
    """The git tree object for *path* at commit *sha*.

    ``"."`` (and the empty path) resolve to the commit's root tree, matching
    exactly how the release flow writes the anchor's content half.
    """
    rev = f"{sha}^{{tree}}" if path in (".", "") else f"{sha}:{path}"
    try:
        result = effects.run(
            ["git", "rev-parse", rev],
            capture_output=True, text=True, timeout=_READ_TIMEOUT, cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise RlsblError(
            f"could not read the git tree {rev!r} while remapping release "
            f"anchors: {exc}"
        ) from exc
    if effects.unsettled(result) or getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise RlsblError(
            f"could not resolve the git tree {rev!r} while remapping release "
            f"anchors{': ' + detail if detail else ''}. The rewritten commit "
            f"must be readable before its anchor can be recorded."
        )
    return (result.stdout or "").strip()


def _content_mismatch(version, path, old_sha, new_sha, recorded, found, archive):
    return RlsblError(
        f"the release anchor for {version} cannot be moved through the "
        f"rewrite: the content it recorded is not the content the rewritten "
        f"commit carries.\n"
        f"  released path:    {path}\n"
        f"  recorded tree:    {recorded}\n"
        f"  tree at {new_sha[:12]}:  {found}\n"
        f"  anchor moved:     {old_sha[:12]} -> {new_sha[:12]}\n"
        f"  archive:          {archive}\n"
        f"  The archive states what {version} SHIPPED. Re-anchoring it to a "
        f"commit whose tree differs would make it claim content that was never\n"
        f"  released -- which is exactly what a rewrite that redacted a "
        f"released file produces. Nothing has been written.\n"
        f"  Decide what the record should say (the shipped artifact is "
        f"unchanged on the registry; only the repository's history moved), "
        f"then\n"
        f"  edit the archive through `rlsbl.release_file.writable_release_file` "
        f"or record the version as unanchorable."
    )


def plan_anchor_remap(releases_dir: str, commit_map: dict, *, cwd=None,
                      on_content_change=ON_CONTENT_CHANGE_REFUSE):
    """Which archives the rewrite moves, verified but not yet written.

    Reads every archive, maps the anchors, recomputes and compares the tree
    hashes, and returns one :class:`AnchorRemap` per version that moves.

    *on_content_change* declares what a released path whose tree differs at the
    rewritten commit means to this caller: ``"refuse"`` raises
    :class:`~rlsbl.errors.RlsblError` naming the version and both hashes, so the
    caller writes nothing; ``"record"`` recomputes the tree and reports the
    change on the returned item's ``changed_paths``. See the module docstring
    for why ``rlsbl release scrub`` is the caller that declares the second.
    """
    if on_content_change not in ON_CONTENT_CHANGE:
        raise ValueError(
            f"on_content_change must be one of {ON_CONTENT_CHANGE}, got "
            f"{on_content_change!r}"
        )
    if not commit_map:
        return []

    planned = []
    for version in list_archived_versions(releases_dir):
        path = archived_release_path(releases_dir, version)
        archive = read_release_file(path)
        if archive.unanchorable:
            continue
        old_sha = (archive.candidate_sha or "").strip()
        if not old_sha:
            # An archive carrying neither an anchor nor the unanchorable marker
            # is the ledger's MISSING-ANCHOR case, which has its own recovery
            # and its own error. A rewrite has nothing to move here.
            continue
        new_sha = _map_sha(old_sha, commit_map)
        if not new_sha or new_sha == old_sha:
            continue

        recorded_trees = archive.tree_hashes or {}
        new_trees = {}
        changed = []
        for tree_path, recorded in recorded_trees.items():
            found = _tree_at(new_sha, tree_path, cwd=cwd)
            if found != recorded:
                if on_content_change == ON_CONTENT_CHANGE_REFUSE:
                    raise _content_mismatch(
                        version, tree_path, old_sha, new_sha, recorded, found,
                        path,
                    )
                changed.append((tree_path, recorded, found))
            new_trees[tree_path] = found
        planned.append(AnchorRemap(
            version=version, path=path, old_sha=old_sha, new_sha=new_sha,
            tree_hashes=new_trees, changed_paths=tuple(changed),
        ))
    return planned


def remap_release_anchors(releases_dir: str, commit_map: dict, *, cwd=None,
                          on_content_change=ON_CONTENT_CHANGE_REFUSE):
    """Move every archived anchor in *releases_dir* through *commit_map*.

    Returns the :class:`AnchorRemap` records for the archives that moved, in
    the ledger's own highest-first order. The whole set is planned and verified
    before the first write, so a mismatch on any version leaves every archive
    untouched.

    Each write unlocks the read-only archive, rewrites the anchor, and relocks
    it -- the documented edit path, so the archive is never observable as a
    writable anchored file.
    """
    planned = plan_anchor_remap(
        releases_dir, commit_map, cwd=cwd, on_content_change=on_content_change,
    )
    for remap in planned:
        with writable_release_file(remap.path):
            write_release_anchor(
                remap.path,
                candidate_sha=remap.new_sha,
                tree_hashes=remap.tree_hashes,
            )
    return planned


def anchor_remap_event(rewrite: str, remaps):
    """The lineage event recording an anchor remap, or None when nothing moved.

    The record is what lets a later reader EXPLAIN why a version's archive
    names a commit that no earlier record mentions -- including a reader in a
    fresh clone, where safegit's own journal (which lives under ``.git``) is
    not there to consult.
    """
    from .lineage import AnchorMapping, AnchorRemapEvent

    mappings = [
        AnchorMapping(old_sha=r.old_sha, new_sha=r.new_sha) for r in remaps
    ]
    if not mappings:
        return None
    return AnchorRemapEvent(rewrite=rewrite, mappings=mappings)


def record_anchor_remap(lineage_path: str, rewrite: str, remaps):
    """Append the anchor-remap event for *remaps* to the lineage record.

    Returns the path when an event was written, None when nothing moved.
    """
    event = anchor_remap_event(rewrite, remaps)
    if event is None:
        return None
    from .lineage import append_event

    append_event(lineage_path, event)
    return lineage_path


def releases_dirs_for(project_root: str, workspace_root=None,
                      workspace_projects=None):
    """Every release-archive directory a rewrite in this repository can touch.

    Mirrors :func:`rlsbl.changelog.files.enumerate_changelog_dirs` -- the same
    walk the JSONL remap already takes -- so the two halves of the repair cover
    the same set of projects rather than each deciding for itself.

    A directory is included only when it actually HOLDS an archive. Its mere
    existence proves nothing: several unrelated paths (the release lock, a
    scaffold) create ``releases/`` before anything is released, and treating an
    empty one as a ledger would make every caller pay for a rewrite-journal
    read on a repository with no release to repair.
    """
    from .changelog.files import enumerate_changelog_dirs
    from .ledger import releases_dir_for_changes_dir

    dirs = []
    for changes_dir in enumerate_changelog_dirs(
        str(project_root), workspace_root, workspace_projects=workspace_projects,
    ):
        releases_dir = releases_dir_for_changes_dir(changes_dir)
        if releases_dir in dirs or not os.path.isdir(releases_dir):
            continue
        if list_archived_versions(releases_dir):
            dirs.append(releases_dir)
    return dirs


def lineage_path_for_releases_dir(releases_dir: str) -> str:
    """The lineage record that pairs with a release-archive directory.

    ``.rlsbl/releases/`` -> ``.rlsbl/lineage.jsonl``, and a releasable's
    ``<releasable>/releases/`` -> ``<releasable>/lineage.jsonl``. Both are the
    homes :func:`rlsbl.lineage.get_lineage_path` resolves, expressed as the
    derivation from the directory a caller already holds.
    """
    from .lineage import LINEAGE_FILENAME

    return os.path.join(
        os.path.dirname(os.path.normpath(releases_dir)), LINEAGE_FILENAME,
    )


def repair_anchors(*, project_root, commit_map, rewrite_id,
                   workspace_root=None, workspace_projects=None, cwd=None,
                   on_content_change=ON_CONTENT_CHANGE_REFUSE):
    """Move every ledger anchor in this repository and record that it happened.

    The whole-repository half of the repair: it finds every release-archive
    directory the same walk the JSONL remap uses finds, remaps each one's
    anchors through *commit_map*, and appends an ``anchor-remap`` lineage event
    beside each ledger that moved.

    Returns ``(remaps, touched)`` -- the :class:`AnchorRemap` records across
    every ledger, and the repo paths a commit must carry (the rewritten
    archives plus the lineage records that now name them).

    Verification runs across ALL ledgers before the first write, so a content
    mismatch in one project leaves every project's archives untouched.
    """
    releases_dirs = releases_dirs_for(
        project_root, workspace_root, workspace_projects,
    )
    planned = [
        (d, plan_anchor_remap(d, commit_map, cwd=cwd,
                              on_content_change=on_content_change))
        for d in releases_dirs
    ]

    remaps = []
    touched = []
    for releases_dir, per_dir in planned:
        if not per_dir:
            continue
        for remap in per_dir:
            with writable_release_file(remap.path):
                write_release_anchor(
                    remap.path,
                    candidate_sha=remap.new_sha,
                    tree_hashes=remap.tree_hashes,
                )
            touched.append(remap.path)
        remaps.extend(per_dir)
        lineage_path = record_anchor_remap(
            lineage_path_for_releases_dir(releases_dir), rewrite_id, per_dir,
        )
        if lineage_path:
            touched.append(lineage_path)
    return remaps, touched
