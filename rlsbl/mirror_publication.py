"""Publishing one released version onto a subtree mirror.

A mirror is the standalone repository a monorepo sub-project's subtree is split
into. Its BRANCH is the mirror reconciler's business
(:mod:`rlsbl.commands.monorepo.mirror_cmd`): tool-owned, force-pushed with a
lease, exactly one scaffold commit atop the current split. Its TAGS and its
GitHub Releases are this module's, and nothing else writes them -- a mirror
scaffold deliberately ships no publish workflow, and the reconciler sweeps any
publish workflow that reaches the mirror another way (an older scaffold layer's
leftover, or one carried in by the subtree split), so a mirror never releases
itself.

The commit correspondence
-------------------------

A version's commit on the mirror is derived, never guessed. The release record
anchors every released version to the monorepo commit it shipped from (the
archive's ``candidate_sha``, the commit CI proved green), and the subtree split
is a deterministic function from a monorepo commit to the mirror commit
carrying that commit's subtree state::

    mirror commit for version V  ==  git subtree split --prefix <path> <anchor(V)>

The split is incremental and content-derived, so the split of an ancestor is an
ancestor of the split of HEAD: a tag materialized this way always names a commit
the converged mirror already carries. Nothing here computes a commit any other
way, and nothing accepts one from a caller.

What the Release carries
------------------------

The document is :mod:`rlsbl.release_publication`'s -- the same authority the
monorepo's own Release uses, so the two can never disagree about what a Release
body looks like. One thing differs, and it is the point of the correspondence
above: the ``rlsbl-ci-sha`` marker on a MIRROR Release names the SPLIT commit,
because a marker naming the monorepo's anchor would name a commit that does not
exist in the repository the Release is attached to.

Idempotence, and the one refusal
--------------------------------

Everything here can be re-run: a tag already at the right commit is left alone,
and a Release that already exists has its marker reconciled rather than being
created a second time. The single refusal is a tag that already exists on the
mirror at a DIFFERENT commit -- a released tag is never moved, so that is a hard
error naming both commits and the operator decides.
"""

from __future__ import annotations

import os

from . import effects
from .errors import RlsblError
from .release_publication import (
    create_release,
    ensure_marker,
    publication,
    read_release_body,
)


#: Seconds a subtree split of one commit may take before it is a timeout.
#: A split walks the history once and is cached in ``.git/subtree-cache``, so
#: the first call on a large repository is the slow one.
SPLIT_TIMEOUT = 600

#: Seconds a push of one tag ref, or a remote ref listing, may take.
PUSH_TIMEOUT = 180


class MirrorPublicationError(RlsblError):
    """A mirror tag or Release could not be published."""


# ---------------------------------------------------------------------------
# The commit correspondence
# ---------------------------------------------------------------------------


def split_commit_for(root, subtree_path, source_sha, *, timeout=SPLIT_TIMEOUT):
    """The mirror commit corresponding to monorepo commit *source_sha*.

    A branchless ``git subtree split`` AT that commit: it prints the synthetic
    commit's SHA, creates no ref, and materializes the split ancestry as loose
    objects in the monorepo's object store. Deterministic, so two callers asking
    the same question get the same answer, and the answer for an ancestor is an
    ancestor of the answer for HEAD.

    ``--prefix`` and the path are separate tokens because that is the spelling
    rlsbl's observe allowlist pins (see :mod:`rlsbl.observe_allowlist`); the
    stuck ``--prefix=<path>`` form would be refused above a preview's no-writes
    line.
    """
    result = effects.run(
        ["git", "subtree", "split", "--prefix", subtree_path, source_sha],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MirrorPublicationError(
            f"could not split '{subtree_path}' at {source_sha[:12]} to find "
            f"the mirror's commit for it: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise MirrorPublicationError(
            f"splitting '{subtree_path}' at {source_sha[:12]} produced no "
            f"commit, so the mirror has no commit for it."
        )
    return lines[-1]


def split_map_for(root, subtree_path, source_shas, *, timeout=SPLIT_TIMEOUT):
    """``{source sha: split sha}`` for every commit in *source_shas*.

    One split per distinct commit. The subtree cache makes everything after the
    first walk cheap, and asking per commit is what keeps the map HONEST: each
    entry is the split git itself computed for that commit, never an offset
    guessed from another entry.
    """
    mapping = {}
    for sha in source_shas:
        if not sha or sha in mapping:
            continue
        mapping[sha] = split_commit_for(
            root, subtree_path, sha, timeout=timeout,
        )
    return mapping


# ---------------------------------------------------------------------------
# The mirror's tag namespace
# ---------------------------------------------------------------------------


def mirror_tag(version, *, target):
    """The tag the mirror carries for *version*.

    The mirror is a STANDALONE repository, so the tag is the target's
    standalone form (``v1.2.3``) -- never the workspace's ``{name}@v{version}``
    scheme, which is exactly what a consumer resolving the mirror by URL cannot
    read.
    """
    return target.tag_format(version)


def remote_refs(remote, cwd, *, timeout=PUSH_TIMEOUT):
    """Every ref the mirror remote carries, as ``{refname: sha}``.

    Peeled tag entries (``refs/tags/x^{}``) replace their annotated-tag object
    with the commit the tag points at, which is the sha a comparison against a
    split commit has to use.
    """
    result = effects.run(
        ["git", "ls-remote", remote],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MirrorPublicationError(
            f"could not list the mirror's refs at {remote}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return parse_ls_remote(result.stdout)


def parse_ls_remote(text):
    """``{refname: sha}`` from ``git ls-remote`` output, peeling tags."""
    refs = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if ref.endswith("^{}"):
            refs[ref[: -len("^{}")]] = sha
        elif ref not in refs:
            refs[ref] = sha
    return refs


def remote_tag_commits(refs):
    """``{tag name: commit}`` from a ref map."""
    prefix = "refs/tags/"
    return {
        ref[len(prefix):]: sha
        for ref, sha in refs.items()
        if ref.startswith(prefix)
    }


def push_tag(remote, commit, tag, cwd, *, timeout=PUSH_TIMEOUT):
    """Create *tag* on the mirror at *commit*. Never moves an existing one.

    ``--no-verify``: the monorepo's pre-push hook guards ITS changelog
    coverage, which says nothing about a derived artifact's tag namespace. This
    is the tool declining its own hook on an internal operation, not a
    user-facing escape hatch.
    """
    result = effects.run(
        ["git", "push", "--no-verify", remote, f"{commit}:refs/tags/{tag}"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MirrorPublicationError(
            f"could not push tag {tag} to the mirror at {remote}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def ensure_tag(remote, commit, tag, cwd, *, existing=None, timeout=PUSH_TIMEOUT):
    """Put *tag* on the mirror at *commit*. Returns what it did.

    ``"present"`` when the mirror already carries the tag at exactly that
    commit, ``"pushed"`` when it was created. A tag standing at a DIFFERENT
    commit is a hard error: a released tag names what shipped, and moving one is
    never this module's decision.

    *existing* is an already-read ``{tag: commit}`` map, so a caller
    materializing several tags reads the remote once.
    """
    if existing is None:
        existing = remote_tag_commits(remote_refs(remote, cwd, timeout=timeout))
    at = existing.get(tag)
    if at == commit:
        return "present"
    if at is not None:
        raise MirrorPublicationError(
            f"the mirror at {remote} already carries tag {tag} at {at[:12]}, "
            f"but this version's subtree split is {commit[:12]}. A released "
            f"tag is never moved: either the mirror's history was rewritten "
            f"under the tag, or the tag names a different release. Resolve it "
            f"by hand -- rlsbl will not choose which commit that version "
            f"shipped from."
        )
    push_tag(remote, commit, tag, cwd, timeout=timeout)
    return "pushed"


# ---------------------------------------------------------------------------
# The GitHub Release
# ---------------------------------------------------------------------------


def publish_release(pub, *, gh, repo, directory="."):
    """Create the mirror's Release for *pub*, or reconcile the existing one.

    Returns ``"created"``, ``"reconciled"`` (the body gained or corrected its
    marker) or ``"already-correct"``.
    """
    try:
        body = read_release_body(pub.tag, gh=gh, repo=repo)
    except Exception:
        create_release(pub, gh=gh, repo=repo, directory=directory)
        return "created"
    if pub.reconciled_body(body) is None:
        return "already-correct"
    ensure_marker(pub, gh=gh, repo=repo, directory=directory)
    return "reconciled"


def version_notes(changes_dir, version):
    """The version's generated changelog section, or ``""``.

    The per-version ``.md`` beside the JSONL is the release's own rendering of
    that version, which is exactly what the monorepo's Release carries -- so the
    mirror's Release says the same thing about the same version.
    """
    path = os.path.join(str(changes_dir), f"{version}.md")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# The whole publication of one version
# ---------------------------------------------------------------------------


def publish_version(
    *,
    remote,
    root,
    subtree_path,
    version,
    tag,
    anchor_sha,
    notes="",
    gh,
    existing_tags=None,
    directory=".",
    log=None,
):
    """Publish one released version onto the mirror: the tag, then the Release.

    *anchor_sha* is the release record's anchor -- the MONOREPO commit the
    version shipped from. The mirror's commit for it is derived here, and it is
    that commit the tag names and the Release's marker carries.

    Returns ``(split_sha, tag_outcome, release_outcome)``. Raises
    :class:`MirrorPublicationError` on anything it cannot do; the caller decides
    how fatal that is (in the release flow, not very -- the primary release has
    already shipped and the mirror is a derived artifact).
    """
    say = log or (lambda _message: None)

    split_sha = split_commit_for(root, subtree_path, anchor_sha)
    say(
        f"Mirror commit for {version}: {split_sha[:12]} "
        f"(split of {anchor_sha[:12]})"
    )

    tag_outcome = ensure_tag(
        remote, split_sha, tag, root, existing=existing_tags,
    )
    say(f"Mirror tag {tag}: {tag_outcome}")

    pub = publication(
        tag=tag, version=version, candidate_sha=split_sha, notes=notes,
    )
    release_outcome = publish_release(
        pub, gh=gh, repo=remote, directory=directory,
    )
    say(f"Mirror Release {tag}: {release_outcome}")
    return split_sha, tag_outcome, release_outcome
