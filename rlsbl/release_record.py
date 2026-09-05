"""The release record: the archived release files, read as the authoritative
record of what this project has released.

Every completed release archives its release file to
``<releases_dir>/v{X.Y.Z}.toml`` and writes the RELEASE COMMIT into it -- the
``candidate_sha`` CI verified and the ``tree_hashes`` each released path
shipped -- or, for a version whose commit could not be recovered at all, the
``unrecoverable`` marker.  Those archives, not the git tags, are what rlsbl
now asks when it needs to know what was released.

The three fates
---------------

Every archive is in exactly ONE of three states, and every read dispatches on
which:

* **recorded** -- ``candidate_sha`` plus ``tree_hashes``.  It shipped, and
  rlsbl knows the commit and the trees it shipped from.
* **unrecoverable** -- ``unrecoverable = true``.  It shipped, and the commit is
  unrecoverable from any source.  The version still HAS consumers and real
  refs; only rlsbl's knowledge of where it came from is gone.
* **never released** -- ``never_released = true``.  The version NUMBER exists
  in the record -- a phantom tag's version, a version claimed and abandoned --
  but no release was ever published under it.

The third is not a degraded second: a never-released version is not a release,
so every read that asks what this project RELEASED skips it.  It is not the
latest release, it does not bound the unreleased range, ``release undo`` does
not select it, its refs are never demanded as missing, and ``release
reconcile`` never plans a deletion of a tag carrying its name.  Its changelog
section is still rendered, annotated as never released, because a phantom
version can have finalized changelog files and hiding them would lose the
record.

Why not tags
------------

``git describe --tags --abbrev=0`` answers "the newest tag reachable from
HEAD", which is a different question from "the newest release contained in
this checkout", and it answers it from a namespace anyone can write.  A tag
that was deleted, moved, or never created makes a released version vanish from
the answer; a tag created by hand makes an unreleased one appear.  The archive
is written by the release flow, is rewritten by rlsbl only through its own
documented unlock paths, and is committed -- so it survives exactly what tags
do not.  (Its local file mode is hygiene, not the guarantee: git records no
read-only bit, so a fresh clone's archives are writable.)

The two questions, and their two different answers
--------------------------------------------------

The release record deliberately answers *two* questions differently, because they are
different questions:

* **What bounds the unreleased range?**  The highest archived version whose
  ``candidate_sha`` is an ANCESTOR of this checkout -- :func:`nearest_release_commit`.
  A release that exists but is not in this history cannot bound a range
  computed from this history.
* **What is the latest release?**  The absolute highest archived version --
  :func:`latest_release_fact` -- annotated when the checkout does not contain
  it.  A fact about the project is not silently rewritten into a fact about
  the checkout; it is stated, with the discrepancy visible.

Reading is lazy and highest-first.  :func:`rlsbl.release_file.list_archived_versions`
costs one ``listdir`` and opens nothing; this module opens archives one at a
time walking down from the top and stops at the first answer.  In the ordinary
case -- a checkout that contains the latest release -- that is exactly one
file.  (Measured on rlsbl's own 224 archives: reading all of them through
``read_release_file`` takes ~340ms, so eagerly loading the release record on every
command was never an option.)

The four read errors
--------------------

They fire where the release record is READ FOR USE, never while scanning:

* **Disagreement** -- the version's tag exists locally and points at a commit
  other than the release commit.  Something moved one of them; the release record will not
  guess which.
* **Indeterminable** -- ancestry cannot be decided (a missing object, a
  truncated history).  Not the same as "no", and not treated as one.
* **No fate at all** -- an archive carrying none of the three: no release
  commit, no ``unrecoverable`` marker, no ``never_released`` marker.  That is
  one written before release commits were recorded and never backfilled.  The
  error prints the complete single-version recovery.
* **Empty release record, tagged repository** -- no archive at all, yet the tag
  namespace carries tags that parse under this project's version-tag scheme.
  That is a project that HAS released and was never backfilled, and reading its
  empty release record would report the entire history as unreleased.  A project with
  no version tags is a project before its first release, and its empty release record
  is the correct answer -- so the two states are told apart by the tags, and
  only the first one raises.

Who does NOT go through these
-----------------------------

``scripts/backfill_release_anchors.py`` -- the remedy the fourth error names --
reads archives and tags directly and never calls the guarded reads, so it can
run on exactly the repository the guard refuses.  So does
``rlsbl release reconcile``, whose observe layer is the tag namespace itself.
Neither needs a bypass, and neither is given one: the structure is what keeps
them clear.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from . import effects
from .errors import ReleaseRecordError
from .git_util import Ancestry, ancestry
from .release_file import (
    archived_release_path,
    list_archived_versions,
    read_release_file,
)
from .tag_glob import TagMode, parse_version_tag


# A git object name as the release commit records it: 7 to 40 hex characters.
_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class ReleaseRecordEntry:
    """One archived release, read for use, in exactly one of three fates.

    * **recorded** -- ``candidate_sha`` names the commit it shipped from;
    * **unrecoverable** -- it shipped, from a commit nothing can name;
    * **never_released** -- the version NUMBER exists, no release does.

    Exactly one holds. An archive carrying none of them raises rather than
    being constructed; one carrying two is refused by the schema.

    ``shipped_as`` is orthogonal to the fate: the historical tag spelling this
    version ACTUALLY shipped under, when it differs from the scheme in effect
    today. None when the archive records none, and always None on a
    never-released entry -- a version nothing shipped shipped under no name.
    """

    version: str
    path: str
    candidate_sha: str | None
    unrecoverable: bool
    never_released: bool = False
    shipped_as: str | None = None

    @property
    def recorded(self) -> bool:
        return self.candidate_sha is not None


@dataclass(frozen=True)
class LatestReleaseFact:
    """The latest release this project has, and whether the checkout has it.

    ``version`` names the highest archived version that was actually RELEASED:
    an archive recorded ``never_released`` is a version number no release ever
    used, so it cannot be the latest release. The ones skipped that way are
    named in ``never_released_above``, highest first, so a display can say what
    it passed over rather than silently reporting an older version.

    ``in_checkout`` is None when the question could not be asked at all: there
    is no release yet, or the latest one is ``unrecoverable`` and so has no
    commit to look for.
    """

    version: str | None
    in_checkout: bool | None
    unrecoverable: bool = False
    never_released_above: tuple[str, ...] = ()

    @property
    def state(self) -> str | None:
        """The fate of the archive ``version`` names: the machine-readable form.

        ``"recorded"``, ``"unrecoverable"``, or None when the project has no
        release at all. A never-released archive never appears here -- it is
        not a release, and it is reported through ``never_released_above``.
        """
        if self.version is None:
            return None
        return "unrecoverable" if self.unrecoverable else "recorded"

    def label(self) -> str:
        """The display string, annotated when the checkout predates the release.

        ``"0.117.2"`` when the checkout contains it, ``"0.117.2 (not in this
        checkout's history)"`` when it does not, ``"(none)"`` when the project
        has released nothing.  A version archived above it but never released
        is named too, because otherwise the display silently reports an older
        version than the highest archive and looks stale.
        """
        if self.version is None:
            base = "(none)"
            notes = []
        elif self.unrecoverable:
            base, notes = self.version, ["commit not recoverable"]
        elif self.in_checkout is False:
            base, notes = self.version, ["not in this checkout's history"]
        else:
            base, notes = self.version, []
        if self.never_released_above:
            notes.append(
                f"{', '.join(self.never_released_above)} archived but never released"
            )
        if not notes:
            return base
        return f"{base} ({'; '.join(notes)})"


def tag_for_version(tag_glob: str | None, version: str) -> str:
    """Translate a version into its tag name under *tag_glob*'s scheme.

    A tag glob is built by replacing ``{version}`` with ``*`` (see
    :mod:`rlsbl.tag_glob`), so putting the version back where the ``*`` is
    reverses the construction for every scheme rlsbl uses: ``v*`` ->
    ``v1.2.3``, ``mylib@v*`` -> ``mylib@v1.2.3``, ``pkg/dir/v*`` ->
    ``pkg/dir/v1.2.3``.  None means the standalone scheme.

    This is tag TRANSLATION, not version selection: it names the tag a version
    would carry, and never decides which version is current.
    """
    glob = tag_glob or "v*"
    return glob.replace("*", version, 1)


def _same_commit(a: str, b: str) -> bool:
    """Do two git object names denote the same commit, allowing abbreviation?"""
    n = min(len(a), len(b))
    return n > 0 and a[:n] == b[:n]


def _resolve_ref(ref: str, cwd: str | None, *, timeout: int = 10) -> str | None:
    """Resolve *ref* to a full commit SHA, or None when it does not resolve."""
    try:
        result = effects.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if effects.unsettled(result) or getattr(result, "returncode", 1) != 0:
        return None
    sha = (result.stdout or "").strip()
    return sha or None


def _missing_release_commit_error(version: str, path: str, tag_glob: str | None,
                          cwd: str | None) -> ReleaseRecordError:
    """Build the MISSING-RELEASE-COMMIT error, with the complete recovery for it.

    The recovery is single-version and mechanical: the value to write is the
    commit the version's own tag already points at, and the archive is
    read-only, so writing it means unlocking, editing and relocking.  Printed
    in full rather than pointing at a document, because the operator hitting
    this has one archive to fix and no reason to go read about the class.
    """
    tag = tag_for_version(tag_glob, version)
    resolved = _resolve_ref(tag, cwd)
    if resolved:
        derived = (
            f'  Its tag "{tag}" points at {resolved}, so that is the\n'
            f"  candidate_sha to record."
        )
        sha_line = f'candidate_sha = "{resolved}"'
    else:
        derived = (
            f'  Its tag "{tag}" does not exist locally, so there is no value to\n'
            f"  derive: fetch the tag (git fetch origin --tags) and re-run, or -- if\n"
            f"  the commit is genuinely gone -- record the version as unrecoverable\n"
            f"  instead (unrecoverable = true, and no candidate_sha/tree_hashes)."
        )
        sha_line = 'candidate_sha = "<the released commit>"'
    return ReleaseRecordError(
        f"the release record entry for {version} records no fate: {path}\n"
        f"  An archive records exactly one of three: the release commit the\n"
        f"  release flow wrote (candidate_sha + [tree_hashes]), the\n"
        f"  unrecoverable marker (it shipped, from a commit nothing can name),\n"
        f"  or the never_released marker (the version number exists, no release\n"
        f"  does). This one records none of them, so it was written before\n"
        f"  release commits were recorded and was never backfilled -- and rlsbl\n"
        f"  cannot tell which commit {version} shipped from, or whether it\n"
        f"  shipped at all.\n"
        f"{derived}\n"
        f"  Recover this one version:\n"
        f"    1. Unlock the archive -- it is chmod 444. In Python:\n"
        f"         from rlsbl.release_file import writable_release_file\n"
        f"         with writable_release_file({path!r}) as p: ...\n"
        f"       or from a shell: chmod 644 {path}\n"
        f"    2. Append the release commit to it:\n"
        f"         {sha_line}\n"
        f"         [tree_hashes]\n"
        f'         "." = "<the tree that commit shipped>"\n'
        f"       (one tree_hashes entry per released path; \".\" for a standalone\n"
        f"       repository, one per member directory for a releasable. Read a\n"
        f"       tree with: git rev-parse <commit>^{{tree}})\n"
        f"    3. Relock it: chmod 444 {path}\n"
        f"    4. Re-run the check -- the next run validates the archive you wrote."
    )


# How many matching tags the empty-release record error prints as evidence. A handful
# names the namespace concretely; the full list is the operator's `git tag -l`
# away, and a count would be a number this message has no reason to carry.
_TAG_EVIDENCE = 3


def _scheme_tags(tag_glob: str | None, cwd: str | None, *,
                 timeout: int = 10) -> list[str]:
    """Local tags matching this project's version-tag scheme, highest first.

    Two filters, because neither alone is the scheme: the glob selects the
    project's own namespace (``v*``, ``lib@v*``, ``pkg/dir/v*``), and
    :func:`~rlsbl.tag_glob.parse_version_tag` keeps only what really parses as
    a version under one of the three schemes -- so ``vNext`` and ``lib@vlatest``
    match the glob and are still not version tags.

    An unanswerable listing (no git, a timeout, a preview past its first
    recorded mutation) yields the empty list, the same reading
    :func:`_resolve_ref` gives an unanswerable resolve: the caller's guard then
    declines to fire rather than accusing a repository on evidence it could not
    read.
    """
    glob = tag_glob or "v*"
    try:
        result = effects.run(
            ["git", "tag", "-l", glob, "--sort=-v:refname"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if effects.unsettled(result) or getattr(result, "returncode", 1) != 0:
        return []
    return [
        tag
        for tag in ((line.strip() for line in (result.stdout or "").split("\n")))
        if tag
        and parse_version_tag(tag, mode=TagMode.PRERELEASE_INCLUSIVE) is not None
    ]


def _unbackfilled_release_record_error(releases_dir: str, tag_glob: str | None,
                               tags: list[str]) -> ReleaseRecordError:
    """Build the EMPTY-RELEASE RECORD error for a repository that has clearly released.

    The remedy is the whole-repository backfill rather than the single-version
    recovery the missing-release-commit error prints: there is no archive to edit here,
    and the versions to materialize are however many the repository shipped.
    """
    glob = tag_glob or "v*"
    evidence = ", ".join(tags[:_TAG_EVIDENCE])
    more = " (and others)" if len(tags) > _TAG_EVIDENCE else ""
    return ReleaseRecordError(
        f"the release record is empty, but this repository has version tags: "
        f"{releases_dir}\n"
        f"  No release archive exists there, so the release record records nothing -- and\n"
        f'  yet the tag namespace carries tags parsing under this project\'s scheme\n'
        f'  ("{glob}"), which is what a project that HAS released and was never\n'
        f"  backfilled looks like.\n"
        f"  Matching tags: {evidence}{more}\n"
        f"  Answering from an empty release record here would report this repository's\n"
        f"  ENTIRE history as unreleased and silently widen every range computed\n"
        f"  from it, so rlsbl refuses instead of answering.\n"
        f"  Backfill the archives -- preview first, then write:\n"
        f"    uv run python scripts/backfill_release_anchors.py --dry-run\n"
        f"    uv run python scripts/backfill_release_anchors.py\n"
        f"  (the script ships in the rlsbl repository: run it from a checkout of\n"
        f"  rlsbl with --repo pointing at this repository's root.)\n"
        f"  A project that has genuinely never released carries no tag under its\n"
        f"  scheme and is unaffected: there, an empty release record IS the answer."
    )


def _require_backfilled_release_record(releases_dir: str, versions: list[str],
                               tag_glob: str | None, cwd: str | None) -> None:
    """Refuse an empty release record in a repository whose tags say it has released.

    A no-op the instant the release record holds anything: a repository mid-backfill,
    or one whose archives predate release-commit recording, is the
    missing-release-commit error's
    business, not this one's.
    """
    if versions:
        return
    tags = _scheme_tags(tag_glob, cwd)
    if not tags:
        return
    raise _unbackfilled_release_record_error(releases_dir, tag_glob, tags)


def version_is_archived(releases_dir: str, version: str) -> bool:
    """Does the release record record *version* as released?

    A scan, not a read: the archive's mere existence is the record that the
    release completed, and answering it opens no file. The read errors belong
    to the callers that go on to USE the entry.
    """
    return os.path.isfile(archived_release_path(releases_dir, version))


def read_entry(releases_dir: str, version: str, *, tag_glob: str | None = None,
               cwd: str | None = None) -> ReleaseRecordEntry:
    """Read one archived release FOR USE, with its read errors live.

    Raises :class:`~rlsbl.errors.ReleaseRecordError` when the archive is in NONE
    of the three fates -- no release commit, no ``unrecoverable`` marker and no
    ``never_released`` marker -- and when the version's tag exists locally but
    points somewhere other than the release commit.  Raises
    ``FileNotFoundError`` when there is no archive for *version* at all --
    that is a caller error, since callers reach here from the enumeration.
    """
    path = archived_release_path(releases_dir, version)
    cfg = read_release_file(path)

    if cfg.never_released:
        return ReleaseRecordEntry(version=version, path=path, candidate_sha=None,
                                  unrecoverable=False, never_released=True)

    if cfg.unrecoverable:
        return ReleaseRecordEntry(version=version, path=path, candidate_sha=None,
                           unrecoverable=True, shipped_as=cfg.shipped_as)

    sha = cfg.candidate_sha
    if not sha or not _HASH_RE.match(sha):
        raise _missing_release_commit_error(version, path, tag_glob, cwd)

    tag = tag_for_version(tag_glob, version)
    tag_commit = _resolve_ref(tag, cwd)
    if tag_commit is not None and not _same_commit(tag_commit, sha):
        raise ReleaseRecordError(
            f"the release record and the git tag disagree about {version}:\n"
            f'  tag "{tag}" points at   {tag_commit}\n'
            f"  the archive's release commit is {sha}\n"
            f"  archive: {path}\n"
            f"  The archive is the record the release flow itself wrote, and rlsbl\n"
            f"  rewrites an archive only through its own documented unlock paths, so\n"
            f"  a disagreement means the TAG moved -- a history rewrite that did not\n"
            f"  re-point it, or a hand-made tag on the wrong commit. (The archive's\n"
            f"  file mode is hygiene, not the guarantee: git records no read-only\n"
            f"  bit, so a fresh clone's archives are writable.) Re-point the tag at\n"
            f"  the release commit (git tag -f\n"
            f"  {tag} {sha}), or, if the rewrite was intended, run "
            f"`rlsbl release reconcile`\n"
            f"  to repair the release metadata from the rewrite journal. rlsbl will\n"
            f"  not guess which of the two is right."
        )

    return ReleaseRecordEntry(version=version, path=path, candidate_sha=sha,
                       unrecoverable=False, shipped_as=cfg.shipped_as)


def _indeterminable_error(entry: ReleaseRecordEntry, head: str) -> ReleaseRecordError:
    return ReleaseRecordError(
        f"cannot determine whether the released commit for {entry.version} is in\n"
        f"  this checkout's history: git could not answer whether "
        f"{entry.candidate_sha}\n"
        f"  is an ancestor of {head}. The commit's objects are missing, or the\n"
        f"  repository's history is truncated so the walk stops before reaching it.\n"
        f"  archive: {entry.path}\n"
        f"  This is not a 'no' and rlsbl will not read it as one -- an unanswerable\n"
        f"  ancestry would silently widen every range computed from it.\n"
        f"  Deepen the repository and re-run:\n"
        f"    git fetch --unshallow          (a shallow clone)\n"
        f"    git fetch origin {entry.candidate_sha}   (a single missing commit)"
    )


def nearest_release_commit(releases_dir: str, *, tag_glob: str | None = None,
                 cwd: str | None = None, head: str = "HEAD") -> ReleaseRecordEntry | None:
    """The highest archived release whose commit this checkout CONTAINS.

    This is what bounds every unreleased-range and coverage computation.  The
    walk is highest-first and stops at the first version whose
    ``candidate_sha`` is an ancestor of *head*, so the ordinary case opens one
    archive.  A version in either commitless fate is skipped: an
    ``unrecoverable`` one has no commit to bound with, and a ``never_released``
    one was never a release at all, so neither can bound a range and their
    neighbours do it instead.

    Returns None when the release record records nothing this checkout contains: a
    project before its first release, or a checkout that predates every
    release it knows about.  "Before its first release" is required to look
    like it -- an empty release record in a repository carrying version tags raises
    instead of widening the range to the whole history.

    Raises :class:`~rlsbl.errors.ReleaseRecordError` for any of the read errors,
    including an ancestry git cannot decide.
    """
    versions = list_archived_versions(releases_dir)
    _require_backfilled_release_record(releases_dir, versions, tag_glob, cwd)
    for version in versions:
        entry = read_entry(releases_dir, version, tag_glob=tag_glob, cwd=cwd)
        if entry.never_released or entry.unrecoverable:
            continue
        verdict = ancestry(entry.candidate_sha, head, cwd)
        if verdict is Ancestry.TRUE:
            return entry
        if verdict is Ancestry.INDETERMINABLE:
            raise _indeterminable_error(entry, head)
    return None


def unreleased_range(releases_dir: str, *, tag_glob: str | None = None,
                     cwd: str | None = None) -> str:
    """The git log range spec for this checkout's unreleased commits.

    ``<candidate_sha>..HEAD`` when the release record records a release this checkout
    contains, and ``HEAD`` when it does not -- the same shape the tag-based
    predecessor produced, computed from the release record instead of from
    ``git describe``.
    """
    entry = nearest_release_commit(releases_dir, tag_glob=tag_glob, cwd=cwd)
    if entry is None:
        return "HEAD"
    return f"{entry.candidate_sha}..HEAD"


def release_at_commit(releases_dir: str, sha: str, *,
                      tag_glob: str | None = None,
                      cwd: str | None = None) -> ReleaseRecordEntry | None:
    """The release *sha* IS, or None when it shipped no version.

    Asked by displays that want to label a commit with its release
    (``rlsbl watch``, which previously used ``git describe --exact-match`` and
    so labelled from the tag namespace).

    Costs ONE archive read, not a scan: the highest release contained in
    *sha*'s own history is *sha* itself exactly when *sha* is a released
    candidate, so :func:`nearest_release_commit` answered at *sha* either names it or
    names an earlier release -- and an earlier one means *sha* shipped nothing.
    """
    entry = nearest_release_commit(releases_dir, tag_glob=tag_glob, cwd=cwd, head=sha)
    if entry is None or not _same_commit(entry.candidate_sha, sha):
        return None
    return entry


def latest_release_fact(releases_dir: str, *, tag_glob: str | None = None,
                        cwd: str | None = None,
                        head: str = "HEAD") -> LatestReleaseFact:
    """The project's latest release, and whether this checkout contains it.

    The absolute highest archived version that was actually RELEASED -- not the
    highest one in this history, and not a higher archive recorded
    ``never_released``, which is a version number no release ever used.  Those
    are skipped and named in the fact's ``never_released_above``, so a display
    can say what it passed over.  When the checkout does not contain the
    release's commit, the fact still names that version and records the
    discrepancy, so a display can annotate it rather than quietly reporting an
    older release as the latest.

    The "no release yet" fact is reported only for a repository that looks like
    one: an empty release record under a tagged version namespace raises rather than
    reporting ``(none)`` for a project that has plainly released.
    """
    versions = list_archived_versions(releases_dir)
    _require_backfilled_release_record(releases_dir, versions, tag_glob, cwd)

    phantoms: list[str] = []
    for version in versions:
        entry = read_entry(releases_dir, version, tag_glob=tag_glob, cwd=cwd)
        if entry.never_released:
            phantoms.append(version)
            continue
        if entry.unrecoverable:
            return LatestReleaseFact(version=version, in_checkout=None,
                                     unrecoverable=True,
                                     never_released_above=tuple(phantoms))

        verdict = ancestry(entry.candidate_sha, head, cwd)
        if verdict is Ancestry.INDETERMINABLE:
            raise _indeterminable_error(entry, head)
        return LatestReleaseFact(version=version,
                                 in_checkout=verdict is Ancestry.TRUE,
                                 never_released_above=tuple(phantoms))

    return LatestReleaseFact(version=None, in_checkout=None,
                             never_released_above=tuple(phantoms))


def require_checkout_contains_latest(releases_dir: str, *,
                                     tag_glob: str | None = None,
                                     cwd: str | None = None,
                                     head: str = "HEAD") -> None:
    """Refuse to prepare a release on a history missing the latest release.

    Releasing from a checkout that does not contain the latest release's
    candidate would ship a version built on top of a history the previous
    release is not in: the new release would silently revert it, and its
    changelog range would cover commits that already shipped.

    A no-op when the release record records nothing, and when the latest release
    is ``unrecoverable`` -- there is no commit to require.  A version recorded
    ``never_released`` is not the latest release at all, so it never reaches
    here: requiring a checkout to contain a release that never happened would
    refuse every release forever.
    """
    fact = latest_release_fact(releases_dir, tag_glob=tag_glob, cwd=cwd, head=head)
    if fact.version is None or fact.in_checkout is not False:
        return
    entry = read_entry(releases_dir, fact.version, tag_glob=tag_glob, cwd=cwd)
    raise ReleaseRecordError(
        f"this checkout does not contain the latest release, {fact.version}.\n"
        f"  Its released commit {entry.candidate_sha} is not an ancestor of "
        f"{head}.\n"
        f"  archive: {entry.path}\n"
        f"  Releasing from here would ship a history the previous release is not\n"
        f"  in -- reverting it -- and the new version's changelog range would\n"
        f"  cover commits that already shipped.\n"
        f"  Bring the checkout up to date first (git pull, or check out the\n"
        f"  release branch), then re-run the release."
    )


def releases_dir_for_changes_dir(changes_dir: str) -> str:
    """The releases directory that pairs with a changelog changes directory.

    ``.rlsbl/changes/`` -> ``.rlsbl/releases/``, and a releasable's
    ``<releasable>/changes/`` -> ``<releasable>/releases/``.  Both layouts put
    the two directories side by side, and every caller that already resolved a
    changes dir gets its release record from here rather than re-deriving the path.
    """
    return os.path.join(os.path.dirname(os.path.normpath(changes_dir)), "releases")
