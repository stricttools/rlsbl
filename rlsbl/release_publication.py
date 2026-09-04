"""The one place a GitHub Release's body, marker and pre-release flag are decided.

A GitHub Release is written by more than one part of rlsbl: the release flow
creates it at step 21, and ``rlsbl release reconcile`` materializes or repairs
it after a rewrite or a partial release. Before this module each site decided
independently what a Release body looks like, which meant the reconcile path
recreated Releases with notes only -- no ``rlsbl-ci-sha`` marker at all -- and
never marked a pre-release version as a GitHub pre-release. A Release recreated
that way is a Release the publish workflow cannot judge.

Three decisions live here, and nowhere else:

* **The notes** are the version's own CHANGELOG.md section, verbatim.
* **The released-commit marker** is ``<!-- rlsbl-ci-sha: <40 hex> -->``, and
  the sha it carries is THE RELEASE RECORD'S ANCHOR for that version -- the
  ``candidate_sha`` the archive records. The marker is a projection of the
  anchor onto the forge, not an independent fact: the publish workflow reads it
  to learn which commit CI proved green, and the archive is what rlsbl itself
  reads for the same question. :func:`anchor_from_release_record` is how a caller that
  does not already hold the anchor obtains it.
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


# The publish workflow's only precise statement of which commit CI must be
# green on. Anchored to its own line so a reconcile REPLACES it rather than
# appending a second one to a body that already carries a stale marker.
CI_SHA_MARKER_RE = re.compile(r"^<!-- rlsbl-ci-sha: [0-9a-f]{40} -->\n?", re.M)


def ci_sha_marker(candidate_sha: str) -> str:
    """The marker line for a released commit.

    *candidate_sha* is the release record's anchor for the version -- the
    commit the archive records as the one CI verified.
    """
    return f"<!-- rlsbl-ci-sha: {candidate_sha.strip()} -->"


def strip_ci_sha_marker(body: str) -> str:
    """*body* with any released-commit marker line removed."""
    return CI_SHA_MARKER_RE.sub("", body or "")


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
        candidate_sha: the release record's anchor for *version*.
    """

    tag: str
    version: str
    candidate_sha: str
    notes: str = ""
    title: str | None = None

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
        """The Release body: the notes, a blank line, then the marker."""
        notes = (self.notes or "").rstrip("\n")
        if not notes:
            notes = f"Release {self.version}"
        return f"{notes}\n\n{self.marker}\n"

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


def publication(*, tag, version, candidate_sha, notes="", title=None):
    """Build the :class:`ReleasePublication` for one version.

    Raises ``ValueError`` when the anchor is missing: a Release written without
    the marker is one the publish workflow cannot judge, and silently omitting
    it is the failure this module exists to make impossible.
    """
    sha = (candidate_sha or "").strip()
    if not sha:
        raise ValueError(
            f"no release anchor for {version}: a GitHub Release carries the "
            f"rlsbl-ci-sha marker naming the commit the release archive "
            f"anchors, and there is nothing to name."
        )
    return ReleasePublication(
        tag=tag, version=version, candidate_sha=sha, notes=notes or "",
        title=title,
    )


def anchor_from_release_record(releases_dir: str, version: str) -> str | None:
    """The release record anchor for *version*, or None when there is none.

    Read from the archive directly rather than through
    :func:`rlsbl.release_record.read_entry`: this is asked on repair paths, where the
    tag and the anchor are expected to disagree and the guarded read's
    DISAGREEMENT error would refuse to answer exactly when the answer is needed
    to end the disagreement. An unanchorable archive answers None -- there is
    no commit to name.
    """
    from .release_file import archived_release_path, read_release_file

    path = archived_release_path(releases_dir, version)
    if not os.path.isfile(path):
        return None
    archive = read_release_file(path)
    if archive.unanchorable:
        return None
    return archive.candidate_sha


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
