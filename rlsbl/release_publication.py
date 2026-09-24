"""The one place a GitHub Release's body, marker and pre-release flag are decided.

A GitHub Release is written by more than one part of rlsbl: the release flow
creates it at step 21, ``rlsbl release reconcile`` materializes or repairs
it after a rewrite or a partial release, and ``rlsbl release edit`` (which the
changelog commands call after changing a released version) re-syncs its notes
through :func:`resynced_body`. Before this module each site decided
independently what a Release body looks like, which meant the reconcile path
recreated Releases with notes only -- no ``rlsbl-ci-sha`` marker at all -- and
never marked a pre-release version as a GitHub pre-release. A Release recreated
that way is a Release the publish workflow cannot judge.

Four decisions live here, and nowhere else:

* **The layout** of a body is :func:`compose_body`: the version's recorded
  notices (``release_notices`` in its release archive, written by ``rlsbl
  release deprecate`` and ``rlsbl release yank``), each followed by a blank
  line, then the notes and the marker. Because the notices are read from the
  repository, a re-sync reproduces a deprecated Release's top unchanged
  instead of erasing it.
* **The notes** are the version's own CHANGELOG.md section, verbatim.
* **The released-commit marker** is ``<!-- rlsbl-ci-sha: <40 hex> -->``, and
  the sha it carries is THE RELEASE RECORD'S RELEASE COMMIT for that version -- the
  ``candidate_sha`` the archive records. The marker is a projection of the
  release commit onto the forge, not an independent fact: the publish workflow reads it
  to learn which commit CI proved green, and the archive is what rlsbl itself
  reads for the same question. :func:`release_commit_from_record` is how a caller that
  does not already hold the release commit obtains it.
* **The pre-release flag** follows the version: a version carrying a
  pre-release segment is a GitHub pre-release.

Everything that talks to ``gh`` is split into an argv builder and a thin action
taking the caller's own runner, so the release flow (scoped ``run_gh``), the
reconciler (the same), and a caller naming another repository with ``--repo``
all compose the same document and differ only in how they reach the forge.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass

from . import effects
from .utils import commit_files


# The publish workflow's only precise statement of which commit CI must be
# green on. Pinned to its own line so a reconcile REPLACES it rather than
# appending a second one to a body that already carries a stale marker.
CI_SHA_MARKER_RE = re.compile(r"^<!-- rlsbl-ci-sha: ([0-9a-f]{40}) -->\n?", re.M)


def ci_sha_marker(candidate_sha: str) -> str:
    """The marker line for a released commit.

    *candidate_sha* is the release record's release commit for the version -- the
    commit the archive records as the one CI verified.
    """
    return f"<!-- rlsbl-ci-sha: {candidate_sha.strip()} -->"


def strip_ci_sha_marker(body: str) -> str:
    """*body* with any released-commit marker line removed."""
    return CI_SHA_MARKER_RE.sub("", body or "")


def ci_sha_from_body(body: str) -> str | None:
    """The sha the first released-commit marker in *body* names, or None."""
    match = CI_SHA_MARKER_RE.search(body or "")
    return match.group(1) if match else None


# What separates one block of a Release body from the next: a notice from the
# notice below it, and the last notice from the notes.
BLOCK_SEPARATOR = "\n\n"


def compose_body(notices, rest: str) -> str:
    """The layout of every Release body: *notices* top to bottom, then *rest*.

    *rest* is the notes-and-marker document (or, for a notice being added to a
    Release, the body the forge already holds). An empty *rest* leaves the
    notices standing alone.
    """
    blocks = [*notices, rest] if rest else list(notices)
    return BLOCK_SEPARATOR.join(blocks)


def markerless_body(version: str, notes: str, *, notices) -> str:
    """The Release body for a version whose release commit nothing names.

    The same notices and notes :attr:`ReleasePublication.body` carries,
    without a marker: a marker is never invented for a Release that has no
    commit to name.
    """
    return compose_body(
        notices, (notes or f"Release {version}").rstrip("\n") + "\n",
    )


def resynced_body(existing: str, *, tag: str, version: str, notes: str,
                  notices) -> str:
    """An existing Release's body with its notes replaced by *notes*.

    The marker *existing* carries is kept, by composing the full
    :class:`ReleasePublication` document around the sha it names; a body
    carrying no marker gets the notes alone and gains none. The notices are
    the version's recorded ones (:func:`release_notices_from_record`), never
    whatever the forge's body happens to start with.
    """
    sha = ci_sha_from_body(existing)
    if sha is None:
        return markerless_body(version, notes, notices=notices)
    return publication(tag=tag, version=version, candidate_sha=sha,
                       notes=notes, notices=notices).body


def is_prerelease(version: str) -> bool:
    """Does *version* carry a pre-release segment?

    The whole rule: semver puts the pre-release channel after a hyphen, and
    rlsbl's version grammar has no other use for one.
    """
    return "-" in (version or "")


@dataclass(frozen=True)
class ReleasePublication:
    """The full document one version's GitHub Release should carry.

    Attributes:
        tag: the git tag the Release is attached to.
        title: the Release title. Defaults to the tag, which is what the
            release flow has always written.
        notes: the version's CHANGELOG.md section, verbatim and without the
            marker. Empty when the changelog has no section for the version;
            :attr:`body` then falls back to naming the version.
        version: the version being published, which decides
            :attr:`prerelease`.
        candidate_sha: the release record's release commit for *version*.
        notices: the version's recorded deprecate/yank notices, top to
            bottom (``release_notices`` in its release archive).
    """

    tag: str
    version: str
    candidate_sha: str
    notes: str = ""
    title: str | None = None
    notices: tuple[str, ...] = ()

    @property
    def release_title(self) -> str:
        return self.title if self.title is not None else self.tag

    @property
    def prerelease(self) -> bool:
        return is_prerelease(self.version)

    @property
    def marker(self) -> str:
        return ci_sha_marker(self.candidate_sha)

    @property
    def body(self) -> str:
        """The Release body: the notices, the notes, a blank line, the marker."""
        notes = (self.notes or "").rstrip("\n")
        if not notes:
            notes = f"Release {self.version}"
        return compose_body(self.notices, f"{notes}\n\n{self.marker}\n")

    def reconciled_body(self, existing: str) -> str | None:
        """*existing* with this publication's marker on it, or None when correct.

        Idempotent by construction: a body already carrying this exact marker
        answers None (nothing to write), and a body carrying a DIFFERENT marker
        has it replaced rather than a second one appended.
        """
        if self.marker in (existing or ""):
            return None
        stripped = strip_ci_sha_marker(existing).rstrip("\n")
        return f"{stripped}\n\n{self.marker}\n"


def publication(*, tag, version, candidate_sha, notices, notes="", title=None):
    """Build the :class:`ReleasePublication` for one version.

    *notices* is required, so no writer can compose a body for a deprecated
    or yanked version and silently drop its notice: a caller writing a
    version's Release passes :func:`release_notices_from_record`, and one
    whose version cannot carry notices passes ``()`` and says why.

    Raises ``ValueError`` when the release commit is missing: a Release written without
    the marker is one the publish workflow cannot judge, and silently omitting
    it is the failure this module exists to make impossible.
    """
    sha = (candidate_sha or "").strip()
    if not sha:
        raise ValueError(
            f"no release commit for {version}: a GitHub Release carries the "
            f"rlsbl-ci-sha marker naming the commit the release archive "
            f"release commits, and there is nothing to name."
        )
    return ReleasePublication(
        tag=tag, version=version, candidate_sha=sha, notes=notes or "",
        title=title, notices=tuple(notices),
    )


def release_commit_from_record(releases_dir: str, version: str) -> str | None:
    """The recorded release commit for *version*, or None when there is none.

    Read from the archive directly rather than through
    :func:`rlsbl.release_record.read_entry`: this is asked on repair paths, where the
    tag and the release commit are expected to disagree and the guarded read's
    DISAGREEMENT error would refuse to answer exactly when the answer is needed
    to end the disagreement.

    Both commitless fates answer None, for different reasons: an
    ``unrecoverable`` archive records a version that SHIPPED from a commit
    nothing can name, and a ``never_released`` one records a version NUMBER no
    release ever used. A caller that must tell the two apart -- to say "the
    commit could not be derived" rather than "there was never a release here"
    -- asks :func:`version_never_released` as well.
    """
    from .release_file import archived_release_path, read_release_file

    path = archived_release_path(releases_dir, version)
    if not os.path.isfile(path):
        return None
    archive = read_release_file(path)
    if archive.unrecoverable or archive.never_released:
        return None
    return archive.candidate_sha


def release_notices_from_record(releases_dir: str, version: str) -> tuple[str, ...]:
    """The deprecate/yank notices *version*'s archive records, top to bottom.

    Empty for a version with no archive or with no notices: nothing recorded
    means nothing to put on top of the body.
    """
    from .release_file import archived_release_path, read_release_file

    path = archived_release_path(releases_dir, version)
    if not os.path.isfile(path):
        return ()
    return tuple(read_release_file(path).release_notices or ())


def version_never_released(releases_dir: str, version: str) -> bool:
    """Does *version*'s archive record that no release ever used the number?

    The distinction :func:`release_commit_from_record` cannot draw, because it
    answers None for every commitless archive: a version that SHIPPED from a
    commit nothing can name, and a version NUMBER no release ever used, both
    have no commit to return. A caller deciding what to say about a version --
    "the commit could not be derived" versus "there was never a release here"
    -- asks this first.

    False for an absent archive: a version with no record at all is not a
    version recorded as never released.
    """
    from .release_file import archived_release_path, read_release_file

    path = archived_release_path(releases_dir, version)
    if not os.path.isfile(path):
        return False
    return bool(read_release_file(path).never_released)


# ---------------------------------------------------------------------------
# The gh surface: argv builders, then thin actions over a caller's runner
# ---------------------------------------------------------------------------


def _repo_args(repo):
    return ["--repo", repo] if repo else []


def view_body_args(tag, *, repo=None):
    """argv reading one Release's body."""
    return ["release", "view", tag, "--json", "body", "-q", ".body",
            *_repo_args(repo)]


def create_args(pub: ReleasePublication, notes_path, *, repo=None):
    """argv creating the Release *pub* describes, notes read from a file."""
    args = ["release", "create", pub.tag, "--title", pub.release_title,
            "--notes-file", notes_path, *_repo_args(repo)]
    if pub.prerelease:
        args.append("--prerelease")
    return args


def edit_notes_args(tag, notes_path, *, repo=None):
    """argv replacing one Release's notes from a file."""
    return ["release", "edit", tag, "--notes-file", notes_path,
            *_repo_args(repo)]


def edit_all_args(tag, notes_path, *, title=None, prerelease=False, repo=None):
    """argv rewriting one Release's whole document: notes, title, flag.

    The pre-release flag is stated in BOTH directions (``--prerelease`` and
    ``--prerelease=false``), never merely omitted: an edit that left it out
    would keep a Release wrongly marked pre-release marked that way, and the
    point of this argv is that what the forge ends up carrying is decided here
    rather than inherited from whatever was there before.
    """
    args = ["release", "edit", tag, "--notes-file", notes_path]
    if title is not None:
        args += ["--title", title]
    args.append("--prerelease" if prerelease else "--prerelease=false")
    return args + _repo_args(repo)


def delete_args(tag, *, repo=None):
    """argv deleting one Release."""
    return ["release", "delete", tag, "--yes", *_repo_args(repo)]


@contextlib.contextmanager
def notes_file(body, *, directory="."):
    """Write *body* to a temporary notes file and yield its path.

    Written-then-renamed like the release flow's own notes file, so ``gh``
    never observes a partially written body, and removed on the way out even
    when the block raises.
    """
    base = os.path.join(
        str(directory), f".rlsbl-notes-{int(time.time() * 1000)}.tmp",
    )
    writing = base + ".writing"
    with effects.open_write(writing, "w", encoding="utf-8") as f:
        f.write(body)
    effects.rename(writing, base)
    try:
        yield base
    finally:
        for path in (base, writing):
            if os.path.exists(path):
                effects.remove(path)


def create_release(pub: ReleasePublication, *, gh, config=None, repo=None,
                   directory="."):
    """Create the Release *pub* describes. Returns the argv that was run."""
    with notes_file(pub.body, directory=directory) as path:
        args = create_args(pub, path, repo=repo)
        gh(args, config=config)
    return args


def update_release(pub: ReleasePublication, *, gh, config=None, repo=None,
                   directory="."):
    """Rewrite an EXISTING Release to exactly the document *pub* describes.

    The edit counterpart of :func:`create_release`, and the same document: a
    Release whose tag was moved by a rewrite keeps its name and its attachment,
    so only the body, the title and the pre-release flag have to be restated.
    Nothing is deleted, so a failure here leaves the old Release in place.

    Returns the argv that was run.
    """
    with notes_file(pub.body, directory=directory) as path:
        args = edit_all_args(
            pub.tag, path, title=pub.release_title,
            prerelease=pub.prerelease, repo=repo,
        )
        gh(args, config=config)
    return args


def read_release_body(tag, *, gh, config=None, repo=None) -> str:
    """The existing Release's body, as ``gh`` reports it."""
    return gh(view_body_args(tag, repo=repo), config=config) or ""


def notice_archive_path(releases_dir: str, version: str) -> str:
    """The archive a deprecate/yank notice for *version* is recorded in.

    Raises ``ValueError`` when the version has no archive: a notice that is
    only on the forge is erased by the next re-sync, which is what recording
    it exists to prevent, so the command refuses before it changes anything.
    """
    from .release_file import archived_release_path

    path = archived_release_path(releases_dir, version)
    if not os.path.isfile(path):
        raise ValueError(
            f"{version} has no release archive at {path}. A deprecate or yank "
            f"notice is recorded in the version's archive so every later "
            f"re-sync of its GitHub Release keeps it; without the archive the "
            f"notice would exist only on GitHub and the next "
            f"`rlsbl release edit` would erase it."
        )
    return path


def publish_release_notice(*, tag, notice, archive_path, commit_message, gh,
                           dry_run, cwd=None):
    """Record *notice* in *archive_path*, commit it, and put it on the Release.

    The Release is also marked pre-release. The notice goes on top of the body
    the forge holds, through :func:`compose_body`, so earlier notices and the
    notes stay as they were. Under *dry_run* the archive write and the commit
    are made through :mod:`rlsbl.effects` (recorded, not performed, in a
    preview) and the forge is left untouched.

    The archive is committed BEFORE the forge is edited: a failed edit then
    leaves the notice recorded, and a later ``rlsbl release edit`` puts it on
    the Release.
    """
    from .release_file import writable_release_file, write_release_notice

    # An unreadable body (no Release body, a network failure) is not fatal:
    # the notice is the point, and it stands on its own.
    try:
        current_body = read_release_body(tag, gh=gh)
    except Exception:
        current_body = ""

    with writable_release_file(archive_path):
        write_release_notice(archive_path, notice)
    commit_files(commit_message, [archive_path], allow_failure=True, cwd=cwd)

    if dry_run:
        return
    with notes_file(compose_body([notice], current_body)) as path:
        gh(edit_all_args(tag, path, prerelease=True))


def ensure_marker(pub: ReleasePublication, *, gh, config=None, repo=None,
                  directory="."):
    """Put *pub*'s marker onto an already-existing Release.

    Returns True when the body was rewritten, False when it already carried
    exactly this marker. Exceptions from *gh* propagate: a Release whose marker
    could not be established is one the publish workflow would judge from
    ``$GITHUB_SHA`` instead, which is a verdict nobody established.
    """
    body = read_release_body(pub.tag, gh=gh, config=config, repo=repo)
    new_body = pub.reconciled_body(body)
    if new_body is None:
        return False
    with notes_file(new_body, directory=directory) as path:
        gh(edit_notes_args(pub.tag, path, repo=repo), config=config)
    return True
