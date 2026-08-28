"""Monorepo mirror reconciler.

The subtree mirror of a monorepo project is a TOOL-OWNED, derived artifact.
Nothing is ever authored on it by hand -- it is regenerated from the monorepo
whenever the project's history advances. Force-push (with lease) is the routine
write, not an exceptional one.

The command is an observe-then-converge reconciler:

* ``rlsbl monorepo mirror <project>``            -- observe, then converge (apply).
* ``rlsbl monorepo mirror <project> --dry-run``  -- observe and report a plan only
  (zero writes beyond the loose objects a branchless ``git subtree split`` leaves
  in the monorepo object store).

Desired state of the mirror's ``main``:

* its tip is exactly one scaffold commit atop the CURRENT split-lineage commit,
  where the split-lineage commit equals the deterministic branchless subtree
  split of the project's current history, and
* the scaffold commit touches only scaffold-owned paths.

One path is swept out of the mirror on every convergence regardless of where it
came from: a publish workflow under ``.github/workflows/``. A mirror never
releases itself -- its tags and Releases are written by the monorepo's release
flow through :mod:`rlsbl.mirror_publication`, and a publish workflow on the
mirror would be a second, unsynchronized publisher of the same versions,
triggered by the reconciler's own pushes. Scaffold's ``publish_mode: "none"``
covers the workflow scaffold would have rendered; the sweep covers the two it
cannot see -- a leftover from an older scaffold layer, and one that rode in
through the subtree split because the member's own directory carries it. A
member that genuinely carries a publish workflow KEEPS IT IN THE MONOREPO,
where it is that member's CI; only the mirror's copy is swept. A tip that still
carries one is classified ``scaffold_stale`` rather than ``converged``, so the
next apply rebuilds the layer without it.

A tripwire enforces the contract with no heuristics: the remote tip must be
EITHER a bare split-lineage commit (the current split SHA or an older one --
covers pre-scaffold-layer mirrors) OR exactly one commit atop a split-lineage
commit whose changed paths are all scaffold-owned. Anything else is a foreign
commit -- a contract violation -- and is a hard error that touches nothing.

The tripwire's walk asks an ancestry question per commit, and that question has
three answers.  "No" and "cannot tell" both keep the walk moving, because a
commit above the split boundary normally CANNOT be answered: its objects live
on the mirror, not in the monorepo.  The difference shows up only when the walk
reaches the end of the tip's history without ever finding a split-lineage
commit.  If every answer along the way was a definite "no", the mirror really
does hold unrelated work: ``contract_violated``.  If any answer was "cannot
tell", the honest verdict is ``lineage_undetermined`` -- also a refusal that
touches nothing, but one that names a git that could not answer (pruned or
unfetched objects) instead of accusing an operator of authoring on the mirror.

The mirror's TAGS are a second dimension of the same reconciler. Every version
the release ledger records shipped from a monorepo commit, and the mirror
carries that version under its own standalone tag name at the subtree split of
that commit. A mirror can be converged on ``main`` and still be missing every
one of those tags -- a mirror bound after the fact, a tag push that failed at
release time, a mirror that was reset -- so the preview names each missing
version and an apply materializes it from the ledger through
:mod:`rlsbl.mirror_publication`.

The observe/preview/apply machinery itself is not this module's: it comes from
:mod:`rlsbl.preview_apply`. A mirror judges the branch and one subject per
released version, so its preview is the keyed verdict list of that module and
its plan output is composed by that module's single renderer.
"""

import json
import os
import sys
from dataclasses import dataclass, field

from .extract import _ensure_git_identity
from ...git_util import Ancestry, ancestry, validate_subtree_remote_ssh_host
from ...mirror_publication import MirrorPublicationError
from ...preview_apply import Preview, Reconciler, VerdictItem, reconcile, single
from ...workspace import (
    find_workspace_root,
    load_releasables,
    load_workspace,
    mirror_remote_for,
    resolve_releasable_for_project,
)
from ... import effects


# ---------------------------------------------------------------------------
# Scaffold-owned path set (the tripwire's allow-list)
# ---------------------------------------------------------------------------
#
# A mirror commit is a legitimate scaffold layer only if every path it changes
# is scaffold-owned. The owned set is the union of:
#
#   1. the keys of the clone's ``.rlsbl/managed-files.json`` manifest
#      (template-derived files tracked for orphan detection -- typically CI
#      workflows under ``.github/``); plus
#   2. the pinned prefixes and root files below.
#
# The pinned set is derived from what ``rlsbl scaffold`` writes (see
# rlsbl/commands/init_cmd.py: apply_plans / _post_scaffold, and rlsbl/tagging.py
# for manifest keyword tagging). Everything the tool owns lives under
# ``.rlsbl/`` (config.json, managed-files.json, version, bases/, changes/,
# hooks/, lint/) or under ``.github/`` (workflows), plus a handful of root
# files. The root files are:
#   * CHANGELOG.md, .gitignore, .npmignore -- template-derived (also in
#     managed-files.json, listed here so the set holds even without a manifest).
#   * LICENSE                              -- scaffolded license file.
#   * package.json, pyproject.toml         -- the ONLY manifests scaffold
#     rewrites, and only to inject the "rlsbl" keyword (rlsbl/tagging.py:
#     ensure_npm_keyword / ensure_pypi_keyword). No other manifest is touched.
# Because the mirror is tool-owned, these directories are entirely scaffold
# territory -- nothing else is ever authored there. Any changed path outside
# this set belongs to the project's own source tree and therefore marks a
# foreign commit.
#
# Pin the set HERE, in one place, and update it if scaffold's outputs change.
#
# One part of the set is NOT pinned but derived: the identity manifests the
# scaffold layer rewrites onto the mirror's own repository identity (go.mod --
# see BaseTarget.mirror_identity_files). The tool writes those files on the
# mirror, so they are scaffold-owned there, and deriving them from the target
# registry means a target that gains an identity manifest cannot turn the
# mirror's own scaffold commit into a foreign-commit accusation.
SCAFFOLD_OWNED_PREFIXES = (".rlsbl/", ".github/")
SCAFFOLD_OWNED_FILES = frozenset({
    "CHANGELOG.md",
    ".gitignore",
    ".npmignore",
    "LICENSE",
    "package.json",
    "pyproject.toml",
})


def scaffold_owned_files():
    """The scaffold-owned root files: the pinned set plus identity manifests."""
    from ...targets import mirror_identity_manifests

    return SCAFFOLD_OWNED_FILES | mirror_identity_manifests()


# Substrings in ``git ls-remote`` stderr that indicate an authentication or
# authorization failure (as opposed to a genuinely missing repository).
_AUTH_MARKERS = (
    "authentication failed",
    "permission denied",
    "could not read username",
    "could not read password",
    "access denied",
    "403 forbidden",
    "terminal prompts disabled",
    "fatal: authentication",
)


class MirrorError(Exception):
    """A hard error in the mirror reconciler (contract violation, auth, etc.)."""


@dataclass
class MirrorPlan:
    """The observed state of a mirror relative to its monorepo source.

    ``state`` is one of:
      * ``"converged"``          -- scaffold commit atop the current split; nothing to do.
      * ``"scaffold_stale"``     -- a scaffold layer atop the CURRENT split, but the
                                    tip carries something the current scaffold would
                                    not leave there: a publish workflow (see
                                    :func:`is_publish_workflow`). Re-scaffolding
                                    sweeps it.
      * ``"behind"``             -- a scaffold layer exists atop an OLDER split; a new
                                    split is available.
      * ``"scaffold_missing"``   -- the tip is a bare split-lineage commit (no scaffold
                                    layer). May also be behind (older split).
      * ``"contract_violated"``  -- a foreign commit exists on the mirror.
      * ``"lineage_undetermined"`` -- the walk never reached a split-lineage
                                    commit AND at least one ancestry question
                                    was unanswerable, so whether the mirror is
                                    foreign was never established.
      * ``"virgin"``             -- the remote is missing or empty.
    """

    state: str
    split_sha: str
    remote_tip: str | None = None
    split_lineage_sha: str | None = None
    behind: bool = False
    foreign_commits: list = field(default_factory=list)  # list of (sha, [paths])
    # Commits whose split-lineage question git could not answer at all.  Only
    # populated on the ``lineage_undetermined`` path: everywhere else an
    # unanswerable commit is an ordinary above-the-boundary commit.
    undetermined_commits: list = field(default_factory=list)
    remote_detail: str = ""
    #: Raw ``git ls-remote`` output, carried so the tags dimension reads the
    #: mirror's tag refs from the SAME listing that classified the branch.
    remote_refs_text: str = ""
    #: Publish workflows found on the mirror's tip. Non-empty is what makes a
    #: tip that is otherwise converged ``scaffold_stale``.
    publish_workflows: list = field(default_factory=list)

    @property
    def split_push_needed(self) -> bool:
        """Whether converging requires pushing a fresh bare split to ``main``.

        ``scaffold_stale`` needs one even though the lineage is already the
        current split: the layer to be swept IS the tip, so it is discarded by
        re-pushing the bare split and rebuilding the layer on top -- which is
        also what keeps the result exactly one commit above the boundary.
        """
        if self.state in ("virgin", "scaffold_stale"):
            return True
        return self.split_lineage_sha != self.split_sha


# ---------------------------------------------------------------------------
# Low-level git helpers (fine-grained exit-code control)
# ---------------------------------------------------------------------------


def _git(args, cwd=None, timeout=180):
    """Run a git command, returning the CompletedProcess (never raises)."""
    return effects.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_ok(args, cwd=None, timeout=180):
    """Run a git command, returning stdout stripped; raise MirrorError on failure."""
    r = _git(args, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        raise MirrorError(
            f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout.strip()


def split_lineage_answer(commit, split_sha, cwd):
    """Is ``commit`` the current split commit or one of its ancestors?

    Returns the :class:`Ancestry` verdict rather than a bool, because the
    reconciler needs all three answers, not two:

    * **TRUE** -- the split boundary; the walk stops here.
    * **FALSE** -- git checked: this commit is not part of the split lineage.
    * **INDETERMINABLE** -- git could not check.  This is the ORDINARY answer
      for a commit above the boundary: the mirror's scaffold layer exists only
      on the remote, so the monorepo has no object to walk.  It is also what a
      pruned or never-fetched split commit produces, which is why the walk
      keeps it and the caller uses it only where the difference is real (see
      :func:`observe`).

    The walk's fail-closed direction is "not lineage" for BOTH non-TRUE
    answers: a commit whose lineage was never established is never treated as
    a boundary, so the reconciler refuses rather than force-pushing over
    something it cannot account for.
    """
    return ancestry(commit, split_sha, cwd=cwd, timeout=180)


# ---------------------------------------------------------------------------
# Observation layer (mutation-free apart from loose split objects)
# ---------------------------------------------------------------------------


def classify_remote(remote, cwd):
    """Classify the remote via ``git ls-remote``.

    Returns a tuple ``(kind, tip, detail)`` where ``kind`` is one of:
      * ``"missing"``   -- ``ls-remote`` failed and stderr does not look like auth.
      * ``"auth"``      -- ``ls-remote`` failed with an authentication/authorization error.
      * ``"empty"``     -- ``ls-remote`` succeeded but the remote has no refs.
      * ``"no_main"``   -- the remote has refs but no ``refs/heads/main``.
      * ``"populated"`` -- ``refs/heads/main`` exists; ``tip`` is its SHA.
    """
    return classify_result(_git(["ls-remote", remote], cwd=cwd))[:3]


def classify_result(r):
    """:func:`classify_remote`'s classification of an already-run ``ls-remote``.

    Returns ``(kind, tip, detail, refs_text)``. The raw output is carried out
    with the verdict so the tags dimension can read the mirror's TAG refs from
    the same listing that decided the branch's state -- one call to the remote,
    two questions answered from it.
    """
    if r.returncode != 0:
        stderr = (r.stderr or "").lower()
        if any(m in stderr for m in _AUTH_MARKERS):
            return ("auth", None, r.stderr.strip(), "")
        return ("missing", None, r.stderr.strip(), "")

    refs = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    if not refs:
        return ("empty", None, "", r.stdout)
    tip = refs.get("refs/heads/main")
    if tip is None:
        return ("no_main", None, "", r.stdout)
    return ("populated", tip, "", r.stdout)


def compute_split_sha(root, project_path):
    """Deterministic branchless subtree split of ``project_path``.

    Runs ``git subtree split --prefix <path>`` WITHOUT ``-b``: it prints the
    resulting commit SHA to stdout, creates no refs, and materializes the whole
    synthetic split lineage as loose objects in the monorepo (so later
    ancestry checks against older split commits resolve locally).

    ``--prefix`` and the path are SEPARATE tokens on purpose: that is the
    spelling the observe allowlist pins (``git subtree split --prefix``), and
    the stuck form ``--prefix=<path>`` -- which git accepts identically --
    would match no prefix, so under ``--dry-run`` this run would be recorded
    instead of observed and the whole preview would truncate on its result.
    """
    r = _git(["subtree", "split", "--prefix", project_path], cwd=root)
    if r.returncode != 0:
        raise MirrorError(
            f"git subtree split failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        raise MirrorError("git subtree split produced no SHA")
    return lines[-1]


def _clone_main(remote, dest):
    """Full single-branch clone of ``main`` so the whole tip layer is inspectable."""
    r = _git(
        ["clone", "--quiet", "--single-branch", "--branch", "main", remote, dest]
    )
    if r.returncode != 0:
        raise MirrorError(
            f"failed to fetch mirror for inspection: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )


def _first_parent_chain(clone_dir, tip):
    """First-parent commit chain from ``tip`` (newest first)."""
    out = _git_ok(["rev-list", "--first-parent", tip], cwd=clone_dir)
    return [c for c in out.splitlines() if c.strip()]


def _changed_paths(clone_dir, base, tip):
    """Paths changed between ``base`` and ``tip`` (name-only)."""
    out = _git_ok(["--no-optional-locks", "diff", "--name-only", base, tip], cwd=clone_dir)
    return [p for p in out.splitlines() if p.strip()]


def _commit_paths(clone_dir, commit):
    """Paths changed by a single ``commit`` (vs its first parent)."""
    r = _git(["show", "--first-parent", "--name-only", "--format=", commit],
             cwd=clone_dir)
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.splitlines() if p.strip()]


# ---------------------------------------------------------------------------
# The one path the mirror must never carry: a publish workflow
# ---------------------------------------------------------------------------
#
# A mirror never releases itself. Its tags and its GitHub Releases are written
# by the monorepo's release flow through rlsbl.mirror_publication; a publish
# workflow on the mirror would be a SECOND, unsynchronized publisher of the
# same versions, triggered by the very pushes the reconciler makes.
#
# Two ways one arrives, and the scaffold's own orphan sweep covers neither:
#
#   * in the mirror's scaffold layer, written by an older scaffold that ran
#     before publish suppression -- the sweep only removes what the CURRENT
#     manifest tracks, and an unmanaged leftover is invisible to it;
#   * through the SPLIT, when the member's own directory carries
#     ``.github/workflows/`` in the monorepo -- those files are the subtree's
#     content, so no scaffold sweep has ever looked at them.
#
# The whole ``.github/`` prefix is already scaffold territory on a mirror (the
# tripwire's SCAFFOLD_OWNED_PREFIXES says so: a commit touching it is a
# legitimate scaffold layer, never a foreign one), so the sweep below is aligned
# with an ownership rule that already existed rather than claiming a new one.
# It runs on the MIRROR CLONE only: the member's own publish workflow stays
# exactly where it is in the monorepo, where it belongs to the member's CI.
WORKFLOWS_PREFIX = ".github/workflows/"


def is_publish_workflow(path):
    """Whether *path* is a publish workflow under ``.github/workflows/``.

    Named, not sniffed: a workflow file whose NAME says publish. That is every
    spelling rlsbl itself renders (``publish.yml``, ``docker-publish.yml``) and
    the spellings a hand-written one uses. Reading a workflow's body to decide
    whether it publishes would be a heuristic; a name is a fact.
    """
    if not path.startswith(WORKFLOWS_PREFIX):
        return False
    name = path[len(WORKFLOWS_PREFIX):]
    if "/" in name:
        return False
    return "publish" in name.lower()


def publish_workflows_in(tree_dir):
    """The publish workflows present in a checked-out tree, as repo paths."""
    workflows = os.path.join(tree_dir, ".github", "workflows")
    if not os.path.isdir(workflows):
        return []
    found = []
    for name in sorted(os.listdir(workflows)):
        path = f"{WORKFLOWS_PREFIX}{name}"
        if is_publish_workflow(path) and os.path.isfile(
            os.path.join(workflows, name)
        ):
            found.append(path)
    return found


def _load_owned_predicate(clone_dir):
    """Build the ``is scaffold-owned?`` predicate for this mirror.

    Reads the clone's ``.rlsbl/managed-files.json`` (if present) and unions its
    keys with the pinned prefixes/files.
    """
    managed = set()
    manifest = os.path.join(clone_dir, ".rlsbl", "managed-files.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            managed = set(data.get("files", {}).keys())
        except (json.JSONDecodeError, OSError):
            managed = set()

    owned_files = scaffold_owned_files()

    def owned(path):
        if path in managed:
            return True
        if path in owned_files:
            return True
        return any(path.startswith(pref) for pref in SCAFFOLD_OWNED_PREFIXES)

    return owned


def observe(remote, root, project_path):
    """Observe the mirror and return a :class:`MirrorPlan`.

    Mutation-free apart from the loose objects the branchless split leaves in
    the monorepo object store.
    """
    split_sha = compute_split_sha(root, project_path)

    kind, tip, detail, refs_text = classify_result(
        _git(["ls-remote", remote], cwd=root)
    )
    if kind == "auth":
        raise MirrorError(
            f"authentication failed reaching subtree remote: {detail or remote}"
        )
    if kind == "missing" or kind == "empty":
        return MirrorPlan(
            state="virgin", split_sha=split_sha, remote_detail=detail,
            remote_refs_text=refs_text,
        )
    if kind == "no_main":
        raise MirrorError(
            f"mirror remote has refs but no 'main' branch: {remote}. "
            "Reset the mirror (delete stray refs) and re-run."
        )

    # Populated: inspect the tip's shape in an isolated temp clone.
    tmpdir = effects.mkdtemp(prefix="rlsbl-mirror-observe-")
    try:
        clone_dir = os.path.join(tmpdir, "mirror")
        _clone_main(remote, clone_dir)

        # Walk the first-parent chain from the tip down to the SPLIT BOUNDARY:
        # the nearest commit that is part of the split lineage (an ancestor of
        # the current split, i.e. an old or current bare split commit). Every
        # commit strictly above the boundary is the mirror's "layer".
        chain = _first_parent_chain(clone_dir, tip)
        boundary = None
        above = []  # commits above the boundary, newest first
        undetermined = []  # commits whose lineage question git could not answer
        for commit in chain:
            verdict = split_lineage_answer(commit, split_sha, cwd=root)
            if verdict is Ancestry.TRUE:
                boundary = commit
                break
            if verdict is Ancestry.INDETERMINABLE:
                undetermined.append(commit)
            above.append(commit)

        if boundary is None:
            # Nothing in the tip's history answered "yes". Two very different
            # situations end up here, and they must not share a verdict:
            #
            #  * every answer was a definite "no" -> the mirror really does
            #    hold work unrelated to this project.
            #  * some answer was "cannot tell" -> whether the mirror is foreign
            #    was never established. Both refuse and touch nothing, but only
            #    the first one may accuse anybody of authoring on the mirror.
            #
            # Below this branch a boundary was PROVEN, so the verdicts that
            # authorize a write (behind, scaffold-missing) never rest on an
            # unanswerable question -- and the unanswerable commits above a
            # proven boundary are the ordinary case, judged by their paths.
            if undetermined:
                # foreign_commits stays EMPTY here: nothing has been shown to
                # be foreign, and a populated field named that is an
                # accusation waiting for the next reader to render it.
                return MirrorPlan(
                    state="lineage_undetermined",
                    split_sha=split_sha,
                    remote_tip=tip,
                    split_lineage_sha=None,
                    undetermined_commits=undetermined,
                    remote_refs_text=refs_text,
                )
            return MirrorPlan(
                state="contract_violated",
                split_sha=split_sha,
                remote_tip=tip,
                split_lineage_sha=None,
                foreign_commits=[(c, _commit_paths(clone_dir, c)) for c in above],
                remote_refs_text=refs_text,
            )

        # (a) No layer: tip is a bare split-lineage commit.
        if not above:
            return MirrorPlan(
                state="scaffold_missing",
                split_sha=split_sha,
                remote_tip=tip,
                split_lineage_sha=boundary,
                behind=boundary != split_sha,
                remote_refs_text=refs_text,
            )

        owned = _load_owned_predicate(clone_dir)

        # (b) Exactly one commit atop the boundary, all scaffold-owned -> the
        #     legitimate scaffold layer.
        if len(above) == 1:
            changed = _changed_paths(clone_dir, boundary, tip)
            foreign_paths = [p for p in changed if not owned(p)]
            if not foreign_paths:
                if boundary == split_sha:
                    # Converged on the split, but the tip may still carry a
                    # publish workflow -- from an older scaffold layer, or
                    # through the split from the member's own directory. A
                    # mirror never releases itself, so that is a stale layer to
                    # re-scaffold, not a converged mirror.
                    stale = publish_workflows_in(clone_dir)
                    if stale:
                        return MirrorPlan(
                            state="scaffold_stale",
                            split_sha=split_sha,
                            remote_tip=tip,
                            split_lineage_sha=boundary,
                            publish_workflows=stale,
                            remote_refs_text=refs_text,
                        )
                    return MirrorPlan(
                        state="converged",
                        split_sha=split_sha,
                        remote_tip=tip,
                        split_lineage_sha=boundary,
                        remote_refs_text=refs_text,
                    )
                return MirrorPlan(
                    state="behind",
                    split_sha=split_sha,
                    remote_tip=tip,
                    split_lineage_sha=boundary,
                    behind=True,
                    remote_refs_text=refs_text,
                )

        # (c) Anything else (multiple commits, or a single non-scaffold commit)
        #     -> foreign layer. Report each commit above the boundary with its
        #     non-scaffold-owned paths (falling back to all its paths).
        foreign = []
        for commit in above:
            paths = _commit_paths(clone_dir, commit)
            non_owned = [p for p in paths if not owned(p)]
            foreign.append((commit, non_owned or paths))
        return MirrorPlan(
            state="contract_violated",
            split_sha=split_sha,
            remote_tip=tip,
            split_lineage_sha=boundary,
            foreign_commits=foreign,
            remote_refs_text=refs_text,
        )
    finally:
        effects.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The tags dimension: released versions the mirror does not carry
# ---------------------------------------------------------------------------
#
# The mirror's BRANCH is what everything above judges. Its TAGS are a second,
# independent subject: every version the release ledger records shipped from a
# monorepo commit, and the mirror carries that version under its own standalone
# tag name at the subtree split of that commit. A mirror can be perfectly
# converged on main and still be missing every one of them -- a mirror bound
# after the fact, a tag push that failed at release time, a mirror reset -- so
# the preview names them and an apply materializes them from the ledger.


@dataclass
class MirrorTagPlan:
    """One released version's presence on the mirror.

    ``state`` is one of:
      * ``"materialize"`` -- the mirror has no such tag; the split of the
        version's ledger anchor is what an apply would push it at.
      * ``"present"``     -- the mirror already carries the tag.
      * ``"unanchored"``  -- the version's archive records no commit, so there
        is nothing to derive a mirror commit from. Reported, never guessed.
    """

    version: str
    tag: str
    state: str
    anchor_sha: str | None = None
    split_sha: str | None = None
    remote_commit: str | None = None
    notes: str = ""


def observe_tags(remote, root, project_path, *, releases_dir, changes_dir,
                 tag_of, remote_refs_text=None):
    """The mirror's tag state for every version the ledger records.

    *tag_of* maps a version to the tag the mirror carries it under (the
    target's standalone form -- see :func:`rlsbl.mirror_publication.mirror_tag`).

    The subtree split is computed ONLY for a version the mirror is missing:
    that is the one case where the answer is needed (it is the commit the apply
    would push at), and splitting once per released version would walk the
    history once per version for a question already answered by the tag being
    there.
    """
    from ...mirror_publication import (
        parse_ls_remote,
        remote_tag_commits,
        split_commit_for,
        version_notes,
    )
    from ...release_file import list_archived_versions
    from ...release_publication import anchor_from_ledger

    versions = list_archived_versions(releases_dir)
    if not versions:
        return []

    if remote_refs_text is None:
        refs = {}
    else:
        refs = parse_ls_remote(remote_refs_text)
    tags = remote_tag_commits(refs)

    plans = []
    for version in versions:
        tag = tag_of(version)
        if tag in tags:
            plans.append(MirrorTagPlan(
                version=version, tag=tag, state="present",
                remote_commit=tags[tag],
            ))
            continue
        anchor = anchor_from_ledger(releases_dir, version)
        if not anchor:
            plans.append(MirrorTagPlan(
                version=version, tag=tag, state="unanchored",
            ))
            continue
        plans.append(MirrorTagPlan(
            version=version, tag=tag, state="materialize",
            anchor_sha=anchor,
            split_sha=split_commit_for(root, project_path, anchor),
            notes=version_notes(changes_dir, version),
        ))
    return plans


def tag_verdict_item(plan):
    """One released version's :class:`VerdictItem` in the mirror's preview."""
    common = dict(key=f"tag:{plan.tag}", state=plan.state, data=plan)
    if plan.state == "present":
        return VerdictItem(
            summary=(
                f"the mirror carries {plan.tag} at "
                f"{(plan.remote_commit or '')[:12]}."
            ),
            **common,
        )
    if plan.state == "materialize":
        return VerdictItem(
            summary=f"the mirror is missing the tag for released {plan.version}.",
            facts=(
                f"release anchor: {plan.anchor_sha[:12]}",
                f"mirror commit (subtree split of the anchor): "
                f"{plan.split_sha[:12]}",
            ),
            actions=(
                f"apply would push {plan.tag} at {plan.split_sha[:12]} and "
                f"create the mirror's GitHub Release for it.",
            ),
            **common,
        )
    return VerdictItem(
        label="unanchored",
        summary=(
            f"released {plan.version} records no commit in its release "
            f"archive, so the mirror's commit for it cannot be derived."
        ),
        detail=(
            "  Nothing is guessed and nothing is written for this version.\n"
            "  Restore the archive's candidate_sha (or accept that this "
            "version predates the ledger) and re-run."
        ),
        **common,
    )


# ---------------------------------------------------------------------------
# The verdict this plan becomes (rendered by rlsbl.preview_apply)
# ---------------------------------------------------------------------------


def _remediation(plan, project_path):
    lines = []
    for sha, paths in plan.foreign_commits:
        if paths is None:
            lines.append(
                f"  - commit {sha[:12]} is not a split-lineage commit and is not "
                f"a single scaffold commit atop one."
            )
        else:
            shown = ", ".join(paths[:8]) + (" ..." if len(paths) > 8 else "")
            lines.append(
                f"  - commit {sha[:12]} touches non-scaffold paths: {shown}"
            )
    body = "\n".join(lines)
    return (
        f"{body}\n"
        f"The mirror is tool-owned and must never be authored on directly.\n"
        f"Remediation: port the change(s) into the monorepo (under '{project_path}') "
        f"and re-run, OR reset the mirror branch (a fresh forced split discards the "
        f"foreign work) and re-run."
    )


def _undetermined_detail(plan, project_path):
    """Remediation for a walk git could not finish.  Never an accusation."""
    lines = [
        f"  - commit {sha[:12]}: git could not tell whether it belongs to the "
        f"split lineage."
        for sha in plan.undetermined_commits
    ]
    body = "\n".join(lines)
    return (
        f"{body}\n"
        f"The mirror's history never reached a split-lineage commit, and at "
        f"least one ancestry question came back unanswerable rather than "
        f"answered 'no'. The usual cause is objects the monorepo does not "
        f"have: pruned by gc, or never fetched (a shallow clone).\n"
        f"This is NOT a finding of foreign work -- nothing was touched, and "
        f"nothing about who authored what has been established.\n"
        f"Remediation: give the monorepo the objects and re-run. "
        f"`git fetch --unshallow` (or `git fetch --deepen=<n>`) if the clone "
        f"is shallow, `git fetch` the mirror remote to bring its commits "
        f"local, and re-run `git subtree split --prefix {project_path}` to "
        f"re-materialize the split lineage gc may have pruned."
    )


def verdict_item(plan, remote, project_path, project_name):
    """The mirror's :class:`VerdictItem` -- one subject, one whole repository.

    The shared renderer prints the headline, then the facts, then the actions,
    then the free-form detail block; the strings below are what it composes.
    """
    common = dict(key=project_name, state=plan.state, data=plan)

    if plan.state == "converged":
        return VerdictItem(
            summary=(f"mirror is up to date (split {plan.split_sha[:12]}, "
                     f"scaffold layer present)."),
            **common,
        )
    if plan.state == "scaffold_stale":
        return VerdictItem(
            summary=(
                "the mirror is at the current split but carries a publish "
                "workflow."
            ),
            facts=(
                f"split: {plan.split_sha[:12]} (current)",
                "publish workflow(s) on the tip: "
                + ", ".join(plan.publish_workflows),
            ),
            actions=(
                "apply would re-push the split (with lease) and rebuild the "
                "scaffold layer without them -- a mirror never releases "
                "itself; its Releases come from the release flow.",
            ),
            **common,
        )
    if plan.state == "behind":
        return VerdictItem(
            summary="a new split is available.",
            facts=(
                f"old split: {plan.split_lineage_sha[:12]}",
                f"new split: {plan.split_sha[:12]}",
            ),
            actions=(
                "apply would force-push the new split (with lease) and re-scaffold.",
            ),
            **common,
        )
    if plan.state == "scaffold_missing":
        if plan.behind:
            return VerdictItem(
                label="scaffold-missing (and behind)",
                summary=(f"tip is a bare split commit "
                         f"{plan.split_lineage_sha[:12]}, older than current "
                         f"split {plan.split_sha[:12]}."),
                actions=(
                    "apply would force-push the new split (with lease) and scaffold.",
                ),
                **common,
            )
        return VerdictItem(
            summary=(f"tip is the current bare split commit "
                     f"{plan.split_sha[:12]} with no scaffold layer."),
            actions=("apply would add the scaffold commit and push.",),
            **common,
        )
    if plan.state == "contract_violated":
        return VerdictItem(
            summary="foreign commit(s) detected on the mirror.",
            detail=_remediation(plan, project_path),
            **common,
        )
    if plan.state == "lineage_undetermined":
        return VerdictItem(
            summary="git could not determine the mirror's lineage.",
            facts=(
                f"remote tip: {plan.remote_tip[:12]}",
                f"current split: {plan.split_sha[:12]}",
                "no commit in the tip's history could be confirmed as part of "
                "the split lineage, and at least one check was unanswerable.",
            ),
            detail=_undetermined_detail(plan, project_path),
            **common,
        )
    if plan.state == "virgin":
        return VerdictItem(
            label="remote-missing-or-empty",
            summary=f"mirror at {remote} is virgin.",
            actions=(
                f"apply would push split {plan.split_sha[:12]} and scaffold CI.",
            ),
            **common,
        )
    # Defensive: a state the builder does not know still renders honestly.
    return VerdictItem(label="unknown state", summary=plan.state, **common)


# ---------------------------------------------------------------------------
# Convergence (apply)
# ---------------------------------------------------------------------------


def _push_bare_split(remote, split_sha, expected_tip, root):
    """Push the bare split commit to ``main``.

    Uses force-with-lease against ``expected_tip`` when the branch already
    exists; a plain push when creating the branch on a virgin remote.
    """
    # --no-verify: the reconciler's writes to a tool-owned mirror are not
    # subject to the monorepo's changelog pre-push guard (an irrelevant check
    # for a derived artifact). This is the tool bypassing its own hook on an
    # internal operation, not a user-facing escape hatch.
    args = ["push", "--no-verify"]
    if expected_tip is not None:
        args += [f"--force-with-lease=main:{expected_tip}"]
    args += [remote, f"{split_sha}:refs/heads/main"]
    r = _git(args, cwd=root)
    if r.returncode != 0:
        raise MirrorError(
            f"failed to push split to mirror: {r.stderr.strip() or r.stdout.strip()}"
        )


def _rewrite_identity(clone_dir, remote, sub_config_path):
    """Move the clone's identity manifests onto the MIRROR's own identity.

    A manifest that names the repository a package lives in is wrong the moment
    it arrives on a different repository: a mirrored Go package whose go.mod
    still says ``host/owner/mono/packages/lib`` cannot be fetched from the
    mirror at all. Which manifests those are, and how to rewrite them, is each
    target's own answer (``mirror_identity_files`` /
    ``rewrite_mirror_identity``) -- there is no target name here.

    A target that cannot derive the mirror's identity raises, and the raise
    stands: leaving a manifest naming the monorepo would publish a mirror
    nobody can resolve.
    """
    from ...member_context import resolve_member_context
    from ...targets import TARGETS

    member = resolve_member_context(
        clone_dir, releasable_config_dir=os.path.dirname(sub_config_path),
    )
    rewritten = []
    for entry in member.targets or ():
        target = TARGETS.get(entry.name)
        if target is None or not target.mirror_identity_files:
            continue
        try:
            rewritten += list(target.rewrite_mirror_identity(clone_dir, remote))
        except Exception as exc:
            raise MirrorError(
                f"could not move this project's {entry.name} identity onto the "
                f"mirror: {exc}"
            ) from exc
    if rewritten:
        print(f"Rewrote mirror identity in: {', '.join(sorted(set(rewritten)))}")
    return rewritten


def _run_scaffold(clone_dir, sub_config_path, remote):
    """Copy the project's ``.rlsbl/config.json`` into the clone and scaffold.

    Runs ``rlsbl scaffold --no-auto-commit`` so the reconciler owns the commit.
    A non-zero scaffold exit is a HARD ERROR (no warn-and-continue).

    The copied config declares ``publish_mode: "none"``, whatever the monorepo
    member declares, and that is not a downgrade of the member's own setting --
    it is a statement about the MIRROR. A mirror's tags and GitHub Releases are
    written by the monorepo's release flow through
    :mod:`rlsbl.mirror_publication`; a publish workflow on the mirror would be
    a second, unsynchronized publisher of the same versions, triggered by
    pushes the reconciler itself makes. Scaffold's own rule for
    ``publish_mode: "none"`` then does the rest: no publish.yml is rendered, and
    an already-scaffolded one is removed by the orphan sweep.
    """
    config = {}
    if os.path.isfile(sub_config_path):
        with open(sub_config_path, encoding="utf-8") as f:
            config = json.load(f)
    config["publish_mode"] = "none"

    clone_rlsbl_dir = os.path.join(clone_dir, ".rlsbl")
    effects.makedirs(clone_rlsbl_dir, exist_ok=True)
    with effects.open_write(os.path.join(clone_rlsbl_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    result = effects.run(
        # -P: suppress CWD injection from ``python -m`` run in the mirror clone dir
        # (a root module shadowing a stdlib/dep name would break rlsbl imports).
        # No confirmation-skip flag: `scaffold` is `mutating` but not
        # `consequential`, so the child never reaches the confirm protocol.
        [sys.executable, "-P", "-m", "rlsbl", "scaffold", "--no-auto-commit"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MirrorError(
            "rlsbl scaffold failed in mirror clone "
            f"(exit {result.returncode}):\n{result.stderr or result.stdout}"
        )


def _sweep_publish_workflows(clone_dir):
    """Delete every publish workflow from the mirror clone, whatever its origin.

    Scaffold's own orphan sweep removes only what the CURRENT manifest tracks,
    so it never sees a leftover from an older scaffold layer nor a workflow that
    rode in through the subtree split from the member's own directory. This one
    covers the whole ``.github/workflows/`` prefix -- already scaffold territory
    on a mirror by the tripwire's rules -- because the invariant it serves has
    nothing to do with who wrote the file: a mirror never releases itself.

    Only the MIRROR's copy is swept. A member that genuinely carries a publish
    workflow in its own subtree keeps it in the monorepo, where it is the
    member's CI; the deletion happens in the throwaway clone and lands in the
    scaffold commit.
    """
    removed = []
    for path in publish_workflows_in(clone_dir):
        effects.remove(os.path.join(clone_dir, path))
        removed.append(path)
    if removed:
        print(
            "Removed publish workflow(s) from the mirror "
            f"({', '.join(removed)}): a mirror never releases itself."
        )
    return removed


def _converge(plan, remote, root, project_path, sub_config_path):
    """Bring the mirror to the desired state. Idempotent; interrupted runs heal."""
    split_sha = plan.split_sha

    # 1. If the remote's split lineage is not the current split, force the new
    #    bare split onto main (with lease). This temporarily strips the scaffold
    #    layer; step 3 re-adds it -- an interrupted run re-observes as
    #    scaffold-missing and heals.
    if plan.split_push_needed:
        expected = None if plan.state == "virgin" else plan.remote_tip
        print(f"Pushing split {split_sha[:12]} to mirror main...")
        _push_bare_split(remote, split_sha, expected, root)

    # 2. Fresh clone at main (== split_sha now).
    tmpdir = effects.mkdtemp(prefix="rlsbl-mirror-apply-")
    try:
        clone_dir = os.path.join(tmpdir, "mirror")
        r = _git(["clone", "--quiet", "--single-branch", "--branch", "main",
                  remote, clone_dir])
        if r.returncode != 0:
            raise MirrorError(
                f"failed to clone mirror for scaffolding: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )

        # The clone commits the scaffold layer, and `git clone` carries over
        # none of the monorepo's LOCAL user.name/user.email. On a machine that
        # configures no global identity the commit below would fail -- AFTER
        # the bare split was force-pushed, leaving the mirror stripped of its
        # scaffold layer. Same fix the conversions use for the same reason.
        _ensure_git_identity(clone_dir, root)

        # 3. Move the identity manifests onto the mirror, scaffold (hard error
        #    on failure), then commit + push the layer. Identity first: the
        #    scaffold reads the manifests it is about to describe.
        _rewrite_identity(clone_dir, remote, sub_config_path)
        print("Scaffolding CI in mirror...")
        _run_scaffold(clone_dir, sub_config_path, remote)
        _sweep_publish_workflows(clone_dir)

        _git_ok(["add", "-A"], cwd=clone_dir)
        status = _git_ok(["--no-optional-locks", "status", "--porcelain"], cwd=clone_dir)
        if not status:
            print(f"Mirror converged (scaffold produced no changes): {remote}")
            return
        _git_ok(["commit", "-m", "chore: scaffold rlsbl CI"], cwd=clone_dir)

        # Fast-forward child of split_sha; force-with-lease guards against races.
        # --no-verify: the scaffold clone has an rlsbl pre-push hook whose
        # changelog-coverage guard is irrelevant to this tool-owned mirror push.
        r = _git(["push", "--no-verify", f"--force-with-lease=main:{split_sha}",
                  "origin", "main"], cwd=clone_dir)
        if r.returncode != 0:
            raise MirrorError(
                f"failed to push scaffold layer to mirror: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )
        print(f"Mirror converged: {remote}")
    finally:
        effects.rmtree(tmpdir, ignore_errors=True)


def converge_branch(remote, root, project_path, sub_config_path):
    """Observe one mirror's branch and converge it. The reusable entry point.

    Same observation and same convergence the command performs, without the
    preview: the release flow's mirror step calls this so a release and a
    ``rlsbl monorepo mirror`` bring the branch to exactly the same state by
    exactly the same code -- including the force-with-lease, the scaffold layer,
    and the two refusals below.

    Returns the observed :class:`MirrorPlan`. Raises :class:`MirrorError` when
    the mirror carries foreign commits or its lineage could not be established:
    both touch nothing, and neither is something a release may decide to
    overwrite.
    """
    plan = observe(remote, root, project_path)
    if plan.state == "lineage_undetermined":
        raise MirrorError(
            "lineage-undetermined -- git could not determine whether the "
            "mirror shares this project's split lineage; nothing was "
            f"touched.\n{_undetermined_detail(plan, project_path)}"
        )
    if plan.state == "contract_violated":
        raise MirrorError(
            "contract-violated -- the mirror has foreign commit(s); nothing "
            f"was touched.\n{_remediation(plan, project_path)}"
        )
    if plan.state == "converged":
        return plan
    _converge(plan, remote, root, project_path, sub_config_path)
    return plan


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def _member_target(root, project, releasable):
    """The target whose standalone tag format the mirror's tags use.

    The FIRST detected target, which is the same one the release flow's own
    tag derivation uses for a monorepo member. A member with no detectable
    target has no tag scheme, and the tags dimension answers nothing for it
    rather than inventing ``v{version}``.
    """
    from ...targets import TARGETS, detect_targets, resolve_releasable_config_dir

    try:
        entries = detect_targets(
            os.path.join(root, project.path),
            releasable_config_dir=resolve_releasable_config_dir(project, root),
        )
    except Exception:
        return None
    for entry in entries:
        target = TARGETS.get(entry.name)
        if target is not None:
            return target
    return None


def _observe_tags(plan, root, project, releasable, remote):
    """The tag plans for this mirror, or none when the ledger cannot be read."""
    from ...mirror_publication import mirror_tag
    from ...workspace_types import get_releasable_dir

    if releasable is None:
        return []
    target = _member_target(root, project, releasable)
    if target is None:
        return []
    state_dir = get_releasable_dir(root, releasable.name)
    return observe_tags(
        remote, root, project.path,
        releases_dir=os.path.join(state_dir, "releases"),
        changes_dir=os.path.join(state_dir, "changes"),
        tag_of=lambda version: mirror_tag(version, target=target),
        remote_refs_text=plan.remote_refs_text,
    )


def _apply_tag(plan, remote, root, project_path):
    """Materialize one released version's tag and Release on the mirror."""
    from ...mirror_publication import publish_version
    from ...utils import run_gh_unscoped

    if plan.state == "present":
        print(f"Mirror already carries {plan.tag}.")
        return
    if plan.state == "unanchored":
        print(
            f"Skipping {plan.tag}: released {plan.version} records no commit "
            f"in its release archive, so the mirror's commit for it cannot be "
            f"derived. Nothing was written.",
            file=sys.stderr,
        )
        return
    publish_version(
        remote=remote,
        root=root,
        subtree_path=project_path,
        version=plan.version,
        tag=plan.tag,
        anchor_sha=plan.anchor_sha,
        notes=plan.notes,
        # Unscoped: --repo names the MIRROR, so this must not inherit the
        # monorepo's own GH_REPO.
        gh=lambda args, config=None: run_gh_unscoped(args),
        log=print,
    )


def _cmd_mirror(flags, project_root):
    """Observe-then-converge reconciler for a project's subtree mirror.

    ``flags["project"]``  -- workspace project name.
    ``flags["dry-run"]``  -- plan only (no writes).
    """
    project_name = flags["project"]
    dry_run = bool(flags.get("dry-run", False))

    root = find_workspace_root(str(project_root))
    if root is None:
        print("Error: No workspace found. Run 'rlsbl monorepo init' first.", file=sys.stderr)
        sys.exit(1)

    projects = load_workspace(root)
    project = None
    for p in projects:
        if p.name == project_name:
            project = p
            break
    if project is None:
        available = ", ".join(p.name for p in projects)
        print(
            f"Error: project '{project_name}' not found in workspace. "
            f"Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    # The mirror's destination is the RELEASABLE's, not the member's: a mirror
    # carries one subtree's whole history, tags and Releases, and the
    # releasable is the unit that owns a version, a changelog and a tag scheme.
    releasables = load_releasables(root, projects)
    subtree_remote = mirror_remote_for(project, releasables)
    if not subtree_remote:
        releasable = resolve_releasable_for_project(project, releasables)
        where = (
            f"releasable '{releasable.name}'" if releasable is not None
            else f"project '{project_name}' (which belongs to no releasable)"
        )
        print(
            f"Error: {where} declares no subtree_remote, so "
            f"'{project_name}' has no mirror.",
            file=sys.stderr,
        )
        print(
            "Declare it on the releasable in "
            ".rlsbl-monorepo/workspace.toml:\n"
            "\n"
            "  [[releasables]]\n"
            f"  name = \"{releasable.name if releasable is not None else '<name>'}\"\n"
            "  subtree_remote = \"<mirror repository URL>\"",
            file=sys.stderr,
        )
        sys.exit(1)

    # SSH host consistency (hard error on mismatch).
    validate_subtree_remote_ssh_host(subtree_remote, root)

    project_path = project.path
    sub_config_path = os.path.join(root, project_path, ".rlsbl", "config.json")
    releasable = resolve_releasable_for_project(project, releasables)

    def _observe():
        plan = observe(subtree_remote, root, project_path)
        items = [
            verdict_item(plan, subtree_remote, project_path, project_name),
        ]
        items += [
            tag_verdict_item(tag_plan)
            for tag_plan in _observe_tags(
                plan, root, project, releasable, subtree_remote,
            )
        ]
        return Preview(tuple(items)) if len(items) > 1 else single(items[0])

    def _apply(item):
        if isinstance(item.data, MirrorTagPlan):
            _apply_tag(item.data, subtree_remote, root, project_path)
            return
        plan = item.data
        if plan.state == "lineage_undetermined":
            print("Error: lineage-undetermined -- git could not determine "
                  "whether the mirror shares this project's split lineage; "
                  "nothing was touched.", file=sys.stderr)
            print(_undetermined_detail(plan, project_path), file=sys.stderr)
            sys.exit(1)
        if plan.state == "contract_violated":
            print("Error: contract-violated -- the mirror has foreign commit(s); "
                  "nothing was touched.", file=sys.stderr)
            print(_remediation(plan, project_path), file=sys.stderr)
            sys.exit(1)
        if plan.state == "converged":
            print(f"Already converged: {subtree_remote}")
            return
        _converge(plan, subtree_remote, root, project_path, sub_config_path)

    # (The command keeps its own refusal printing above rather than calling
    # converge_branch: it exits with a status per refusal, where the release
    # flow's non-fatal step wants the same refusal as an exception.)

    # A mirror preview judges more than one subject now: the branch, and every
    # released version's tag. Keys are shown -- the project name on the branch
    # item and `tag:<tag>` on each version -- because with several items a
    # keyless list would not say which line judged what. (This is why the plan
    # names the project even when there is nothing but the branch to report.)
    reconciler = Reconciler(observe=_observe, apply_item=_apply, show_keys=True)
    try:
        reconcile(reconciler, dry_run=dry_run)
    except (MirrorError, MirrorPublicationError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
