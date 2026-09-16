"""The release step table: one declaration per step a release records.

A release walks a fixed sequence of steps and writes a success or failure
marker for each into ``in-progress.json``.  What each step DOES used to be
described in several independent places -- the ordered name tuples, the fatal
set, the Phase-A executor's own dispatch map, the pre-push rollback's
hand-written artifact list, and the revert order inside ``rlsbl release undo``.
Nothing forced those descriptions to agree, and a release that stalled partway
could leave the repository in a shape no single code path knew how to return
to a releasable state.

This module is the one description.  :data:`RELEASE_STEP_TABLE` is an ordered
tuple of :class:`ReleaseStep` records, and every other path derives from it:

- ``release_state`` derives ``MUTATING_STEPS``, ``POST_RELEASE_STEPS``,
  ``RELEASE_STEPS`` and ``FATAL_STEPS`` from it (and therefore so do
  ``get_missing_steps`` and ``is_state_complete``).
- ``phase_a`` builds its plan-kind dispatch from the kinds the table declares,
  and every ``PlanStep`` validates its ``release_step`` and ``marks`` against
  the table's names at construction time.
- ``rollback`` derives the orphaned-artifact candidates from the files the
  table says each step creates.

What every step must decide
---------------------------

Each record answers four questions, and none of them has a default -- a step
that fails to answer one is a hard error when this module is imported, not a
silently absent capability:

``perform``
    Where the forward work is issued: :class:`PlanIssued` (through the Phase-A
    plan executor, naming the plan kinds that do it) or :class:`DirectlyIssued`
    (inline in the named callable).  Both name a ``performer`` as a
    ``module:attribute`` string that ``tests/test_release_step_table.py``
    resolves by import, so the declaration cannot go stale silently.

``inverse``
    :class:`LocalInverse` (a callable that undoes the step inside the
    repository), :class:`ExternalInverse` (undone only by reaching outside the
    repository, naming what performs it and under what condition), or
    :class:`NoInverse` (nothing undoes it, with the reason).

``probe``
    :class:`LocalProbe` (a callable answering whether this step's artifact
    exists right now, reading ONLY inputs the repository owns -- local git
    refs, files in the repository, committed archives) or
    :class:`NoLocalProbe` (the artifact can only be observed over the network,
    with the reason).  A probe NEVER reaches the network: a check that can
    block a release reads only inputs the repository owns.

``artifacts``
    The repository paths this step CREATES -- the files a pre-push
    ``git reset --hard`` would leave behind as untracked orphans.  Steps that
    create no such file declare :func:`no_artifacts`.

The recorded step names are the state file's format and never change: a state
file written before this table existed is still understood.
"""

import dataclasses
import enum
import os
from collections.abc import Callable


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

#: Steps of the mutating phase (rolled back or resumed on failure).
MUTATING_PHASE = "mutating"

#: Steps of the post-release phase (after the GitHub Release).
POST_RELEASE_PHASE = "post-release"

_PHASES = (MUTATING_PHASE, POST_RELEASE_PHASE)


# ---------------------------------------------------------------------------
# Phase-A plan kinds
#
# The sub-operations the Phase-A plan executor issues.  They live here rather
# than in ``phase_a`` because the table is what binds each of them to the
# release step it records: ``phase_a`` imports these names back and builds its
# dispatch from the kinds the table declares.
# ---------------------------------------------------------------------------

WRITE_RELEASABLE_VERSION = "write-releasable-version"
WRITE_TARGET_VERSION = "write-target-version"
WRITE_MEMBER_VERSIONS = "write-member-versions"
BUMP_SELFDOC = "bump-selfdoc"
ENSURE_KEYWORD = "ensure-keyword"
SYNC_LOCKFILE = "sync-lockfile"
WRITE_MARKER = "write-marker"
CLEAN_ARTIFACTS = "clean-artifacts"
BUILD = "build"
SECRET_SCAN = "secret-scan"
GUARD_UNEXPECTED_FILES = "guard-unexpected-files"
COMMIT = "commit"
SNAPSHOT = "snapshot"
GUARD_FOREIGN_COMMITS = "guard-foreign-commits"
GUARD_CANDIDATE_WINDOW = "guard-candidate-window"
RECORD_CANDIDATE = "record-candidate"
PUSH_CANDIDATE = "push-candidate"


# ---------------------------------------------------------------------------
# The context a step's inverse and probe operate on
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StepContext:
    """Everything a step's inverse or probe is allowed to read.

    Deliberately a closed record rather than the release's own ``ReleaseState``:
    an inverse or a probe that needed something not listed here would be
    reaching for a fact the repository may not own.  Every field is required;
    ``None`` is a legitimate value for the ones a given release genuinely has
    no answer for (a standalone project has no ``workspace_root``, an
    imperative release has no ``releases_dir``), and the inverse or probe that
    needs one says so rather than guessing.
    """

    project_dir: str
    git_root: str
    version: str
    previous_version: str | None
    tag: str | None
    branch: str | None
    #: The pre-release HEAD the mutating phase started from.
    pin_sha: str | None
    #: Resolved ``.rlsbl/changes/`` (the releasable's own, in releasable mode).
    changes_dir: str | None
    #: Resolved ``.rlsbl/releases/`` (the releasable's own, in releasable mode).
    releases_dir: str | None
    #: Workspace root, for the snapshot; None for a standalone project.
    workspace_root: str | None
    #: The releasable's state directory, for target/config resolution.
    releasable_config_dir: str | None
    #: Extra tags the release created alongside ``tag``.
    companion_tags: tuple = ()
    #: The release's ``ProjectContext``, for the target writers that want one.
    project_ctx: object = None


class ArtifactState(enum.Enum):
    """What a local probe found."""

    #: The step's artifact is there.
    PRESENT = "present"
    #: The artifact is not there, and this release would create one.
    ABSENT = "absent"
    #: This release never creates this artifact (no changes dir, no
    #: workspace, no release file), so its absence proves nothing.
    NOT_APPLICABLE = "not-applicable"


# ---------------------------------------------------------------------------
# The four declarations
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PlanIssued:
    """Issued by the Phase-A plan executor, through these plan kinds."""

    performer: str
    kinds: tuple

    def __post_init__(self):
        if not self.kinds:
            raise ValueError(
                f"{self.performer}: a PlanIssued step must name the plan "
                f"kinds that issue it"
            )


@dataclasses.dataclass(frozen=True)
class DirectlyIssued:
    """Issued inline by the named callable."""

    performer: str


@dataclasses.dataclass(frozen=True)
class LocalInverse:
    """Undone inside the repository by ``undo(ctx)``."""

    undo: Callable


@dataclasses.dataclass(frozen=True)
class ExternalInverse:
    """Undone only by reaching outside the repository.

    ``performed_by`` names what does it; ``condition`` states when it is
    possible at all.  Both are required: "reversible" with no condition is
    the claim this field exists to stop anyone making.
    """

    performed_by: str
    condition: str


@dataclasses.dataclass(frozen=True)
class NoInverse:
    """Nothing undoes this step, and ``reason`` says why."""

    reason: str


@dataclasses.dataclass(frozen=True)
class LocalProbe:
    """Answers whether the step's artifact exists, from repository inputs only.

    ``observe(ctx)`` returns an :class:`ArtifactState`.
    """

    observe: Callable


@dataclasses.dataclass(frozen=True)
class NoLocalProbe:
    """The artifact can only be observed over the network."""

    reason: str


_INVERSE_KINDS = (LocalInverse, ExternalInverse, NoInverse)
_PROBE_KINDS = (LocalProbe, NoLocalProbe)
_PERFORM_KINDS = (PlanIssued, DirectlyIssued)


def no_artifacts(ctx):
    """This step creates no file a rollback would have to sweep."""
    return ()


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReleaseStep:
    """One step a release records, and everything that knows what it did."""

    #: The name recorded in the state file. Never changes.
    name: str
    phase: str
    #: Whether a failure in this step aborts the release.
    fatal: bool
    #: Whether the step writes to the repository at all.
    mutates_repository: bool
    perform: object
    inverse: object
    probe: object
    #: ``artifacts(ctx)`` -> repository paths this step creates.
    artifacts: Callable

    def __post_init__(self):
        if self.phase not in _PHASES:
            raise ValueError(
                f"release step {self.name!r}: phase must be one of "
                f"{', '.join(_PHASES)}, got {self.phase!r}"
            )
        if not isinstance(self.perform, _PERFORM_KINDS):
            raise TypeError(
                f"release step {self.name!r}: perform must be PlanIssued or "
                f"DirectlyIssued, got {type(self.perform).__name__}"
            )
        if not isinstance(self.inverse, _INVERSE_KINDS):
            raise TypeError(
                f"release step {self.name!r}: inverse must be LocalInverse, "
                f"ExternalInverse or NoInverse -- a step that cannot be undone "
                f"says so with NoInverse(reason=...), it is never left absent"
            )
        if not isinstance(self.probe, _PROBE_KINDS):
            raise TypeError(
                f"release step {self.name!r}: probe must be LocalProbe or "
                f"NoLocalProbe -- a step whose artifact is only observable "
                f"over the network says so with NoLocalProbe(reason=...), it "
                f"is never left absent"
            )
        if not callable(self.artifacts):
            raise TypeError(
                f"release step {self.name!r}: artifacts must be a callable "
                f"taking a StepContext; declare no_artifacts when the step "
                f"creates no file"
            )

    @property
    def plan_kinds(self):
        """The Phase-A plan kinds that issue this step (empty when inline)."""
        return self.perform.kinds if isinstance(self.perform, PlanIssued) else ()


# ---------------------------------------------------------------------------
# Inverses
# ---------------------------------------------------------------------------


class StepInverseError(Exception):
    """An inverse refused to run because its precondition does not hold."""


def _git(args, *, cwd, check=True):
    """A git call inside a step inverse or probe. Always local."""
    from ... import effects

    return effects.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=check, timeout=120,
    )


def _resolved_targets(ctx):
    """The detected targets of the project this release is about."""
    from ...targets import detect_targets

    return detect_targets(
        ctx.project_dir, releasable_config_dir=ctx.releasable_config_dir,
    )


def undo_version_bump(ctx):
    """Write ``previous_version`` back through every target's own writer.

    The forward step wrote the new version through the target adapters, so the
    inverse writes the old one back the same way rather than reconstructing
    manifests by hand -- the adapter is the only thing that knows which files
    a target's version lives in and how each is formatted.
    """
    from ...targets import TARGETS

    if ctx.previous_version is None:
        raise StepInverseError(
            "cannot undo the version bump: the release did not record the "
            "version it bumped from"
        )
    for entry in _resolved_targets(ctx):
        target = TARGETS.get(entry.name)
        if target is None:
            continue
        target.write_version(entry.path, ctx.previous_version, ctx=ctx.project_ctx)
    if ctx.workspace_root and ctx.releasable_config_dir:
        from ...workspace import write_releasable_version

        write_releasable_version(
            ctx.workspace_root,
            os.path.basename(str(ctx.releasable_config_dir).rstrip(os.sep)),
            ctx.previous_version,
        )


def reset_branch_to_pin(ctx):
    """Return the branch to the commit the release started from.

    The inverse of every commit the mutating phase creates below the candidate
    push: the release commit itself and the snapshot commit that may sit on top
    of it.  Refuses rather than guessing when the pin is not an ancestor of
    HEAD -- something other than this release moved the branch, and resetting
    would destroy it.
    """
    if not ctx.pin_sha:
        raise StepInverseError(
            "cannot reset the branch: the release did not record the commit "
            "it started from"
        )
    merge_base = _git(
        ["merge-base", "--is-ancestor", ctx.pin_sha, "HEAD"],
        cwd=ctx.git_root, check=False,
    )
    if merge_base.returncode != 0:
        raise StepInverseError(
            f"cannot reset the branch to {ctx.pin_sha[:10]}: it is not an "
            f"ancestor of HEAD, so the branch moved outside this release"
        )
    _git(["reset", "--hard", ctx.pin_sha], cwd=ctx.git_root)


def undo_changelog_finalization(ctx):
    """Restore ``{version}.jsonl`` back into ``unreleased.jsonl``."""
    from ...changelog.files import unfinalize_version

    if not ctx.changes_dir or not os.path.isdir(ctx.changes_dir):
        return
    unfinalize_version(ctx.changes_dir, ctx.version)


def undo_release_file_finalization(ctx):
    """Restore ``v{version}.toml`` back to ``unreleased.toml``."""
    from ...release_file import unfinalize_release_file

    if not ctx.releases_dir or not os.path.isdir(ctx.releases_dir):
        return
    unfinalize_release_file(ctx.releases_dir, ctx.version)


def delete_local_tags(ctx):
    """Delete the release's local tag and its companions.

    The LOCAL refs only.  A tag that reached origin is PUSHED's business, and
    PUSHED declares that its own undo reaches outside the repository.
    """
    if not ctx.tag:
        raise StepInverseError(
            "cannot delete the release tag: the release did not record one"
        )
    for name in (ctx.tag, *ctx.companion_tags):
        _git(["tag", "-d", name], cwd=ctx.git_root, check=False)


# ---------------------------------------------------------------------------
# Probes -- repository inputs only, never the network
# ---------------------------------------------------------------------------


def _version_files(ctx):
    """The manifest files the detected targets keep their version in."""
    from ...targets import TARGETS

    paths = []
    for entry in _resolved_targets(ctx):
        target = TARGETS.get(entry.name)
        if target is None:
            continue
        try:
            vfile = target.version_file(entry.path)
        except Exception:
            vfile = None
        if vfile:
            paths.append(os.path.join(entry.path, vfile))
    return paths


def probe_version_bumped(ctx):
    """Do the working-tree manifests read this release's version?"""
    from ...targets import TARGETS

    entries = _resolved_targets(ctx)
    if not entries:
        return ArtifactState.NOT_APPLICABLE
    for entry in entries:
        target = TARGETS.get(entry.name)
        if target is None:
            continue
        try:
            current = target.read_version(entry.path)
        except Exception:
            continue
        if current == ctx.version:
            return ArtifactState.PRESENT
    return ArtifactState.ABSENT


def probe_committed(ctx):
    """Is the version bump COMMITTED, rather than only written?

    The manifests read the new version AND git reports nothing outstanding for
    them: the bump is in a commit.  Both halves are local reads of the
    repository's own files and index.
    """
    if probe_version_bumped(ctx) is not ArtifactState.PRESENT:
        return ArtifactState.ABSENT
    paths = _version_files(ctx)
    if not paths:
        return ArtifactState.NOT_APPLICABLE
    result = _git(
        ["--no-optional-locks", "status", "--porcelain", "--", *paths],
        cwd=ctx.git_root, check=False,
    )
    if result.returncode != 0:
        return ArtifactState.ABSENT
    return (
        ArtifactState.ABSENT if result.stdout.strip() else ArtifactState.PRESENT
    )


def probe_snapshot_regenerated(ctx):
    """Does the committed workspace snapshot match the workspace it describes?"""
    if not ctx.workspace_root:
        return ArtifactState.NOT_APPLICABLE
    from ...snapshot import check_snapshot
    from ...workspace import load_workspace
    from ...workspace_graph import WorkspaceGraph

    projects = load_workspace(ctx.workspace_root)
    if not projects:
        return ArtifactState.NOT_APPLICABLE
    graph = WorkspaceGraph(ctx.workspace_root, projects)
    ok = check_snapshot(ctx.workspace_root, projects, graph)
    return ArtifactState.PRESENT if ok else ArtifactState.ABSENT


def probe_changelog_finalized(ctx):
    """Does ``{version}.jsonl`` exist in the changes dir?"""
    if not ctx.changes_dir or not os.path.isdir(ctx.changes_dir):
        return ArtifactState.NOT_APPLICABLE
    versioned = os.path.join(ctx.changes_dir, f"{ctx.version}.jsonl")
    return (
        ArtifactState.PRESENT if os.path.isfile(versioned)
        else ArtifactState.ABSENT
    )


def probe_release_file_finalized(ctx):
    """Is the release file archived as ``v{version}.toml``?

    A release with neither an archive nor an unreleased.toml never had a
    release file to archive (an imperative invocation), which is why the
    absence of both is NOT_APPLICABLE rather than ABSENT.
    """
    if not ctx.releases_dir or not os.path.isdir(ctx.releases_dir):
        return ArtifactState.NOT_APPLICABLE
    versioned = os.path.join(ctx.releases_dir, f"v{ctx.version}.toml")
    if os.path.isfile(versioned):
        return ArtifactState.PRESENT
    unreleased = os.path.join(ctx.releases_dir, "unreleased.toml")
    return (
        ArtifactState.ABSENT if os.path.isfile(unreleased)
        else ArtifactState.NOT_APPLICABLE
    )


def probe_tagged(ctx):
    """Does the release's tag exist as a LOCAL ref?"""
    if not ctx.tag:
        return ArtifactState.NOT_APPLICABLE
    result = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/tags/{ctx.tag}"],
        cwd=ctx.git_root, check=False,
    )
    return (
        ArtifactState.PRESENT if result.returncode == 0
        else ArtifactState.ABSENT
    )


# ---------------------------------------------------------------------------
# Artifact declarations
# ---------------------------------------------------------------------------


def changelog_finalization_artifacts(ctx):
    """The files the changelog finalization creates in the changes dir."""
    if not ctx.changes_dir:
        return ()
    return (
        os.path.join(ctx.changes_dir, f"{ctx.version}.jsonl"),
        os.path.join(ctx.changes_dir, f"{ctx.version}.md"),
    )


def release_file_finalization_artifacts(ctx):
    """The files the release-file finalization creates in the releases dir."""
    if not ctx.releases_dir:
        return ()
    return (
        os.path.join(ctx.releases_dir, f"v{ctx.version}.toml"),
        os.path.join(ctx.releases_dir, f"v{ctx.version}.md"),
    )


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_PHASE_A = "rlsbl.commands.release.phase_a:execute_phase_a_plan"
_MUTATING = "rlsbl.commands.release.execute:_run_release_mutating"

# Main-as-candidate ordering: the release publishes the version-bump commit to
# the release branch UNTAGGED (BRANCH_PUSHED), waits for the repository's own
# CI to go green on exactly that commit (CI_VERIFIED), and only then finalizes
# the changelog / release file, tags the verified commit, pushes the tag, and
# creates the GitHub Release. Nothing irreversible or publicly visible as a
# release exists while CI is red, so a red candidate is fixed forward on the
# same version instead of burning it.
#
# SNAPSHOT_REGENERATED comes BEFORE BRANCH_PUSHED: the monorepo snapshot commit
# must be part of the candidate CI verifies (and therefore of the tagged tree).
#
# "Fatal" means the release stops; it does NOT mean the same recovery for every
# step. Fatal steps split into two tiers around the CANDIDATE PUSH:
#
#   - Pre-push fatal steps (VERSION_BUMPED, COMMITTED, SNAPSHOT_REGENERATED):
#     a failure ROLLS BACK -- `git reset --hard` to the pre-release HEAD plus
#     orphan-artifact cleanup -- leaving the tree as if the release never
#     started. Nothing left the machine.
#   - Post-push fatal steps (BRANCH_PUSHED onward, plus ASSETS_UPLOADED and
#     PIPELINES_PUBLISHED): NO rollback. The candidate commit is on the remote,
#     so a local reset would diverge from it. The failure is recorded and
#     `rlsbl release resume` re-attempts from the failed step via idempotent
#     guards. A red CI_VERIFIED is the canonical case: fix forward on the
#     release branch and resume at the SAME version.
#
# Subtree/mirror, deploy and post-release hooks are NON-FATAL: the release is
# never rolled back for them and the published artifacts stand. Non-fatal does
# NOT mean "exit 0" -- their failure markers reach the completion epilogue,
# which reports them, keeps the state file, and exits nonzero so
# `rlsbl release resume` can re-attempt exactly those steps.
RELEASE_STEP_TABLE = (
    ReleaseStep(
        name="VERSION_BUMPED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=PlanIssued(performer=_PHASE_A, kinds=(
            WRITE_RELEASABLE_VERSION,
            WRITE_TARGET_VERSION,
            WRITE_MEMBER_VERSIONS,
            BUMP_SELFDOC,
            ENSURE_KEYWORD,
            SYNC_LOCKFILE,
            WRITE_MARKER,
            CLEAN_ARTIFACTS,
            BUILD,
            SECRET_SCAN,
            GUARD_UNEXPECTED_FILES,
        )),
        inverse=LocalInverse(undo_version_bump),
        probe=LocalProbe(probe_version_bumped),
        # The manifests are MODIFIED, not created: a reset restores them.
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="COMMITTED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=PlanIssued(performer=_PHASE_A, kinds=(COMMIT,)),
        inverse=LocalInverse(reset_branch_to_pin),
        probe=LocalProbe(probe_committed),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="SNAPSHOT_REGENERATED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=PlanIssued(performer=_PHASE_A, kinds=(SNAPSHOT,)),
        # The snapshot commit sits on top of the release commit, so the same
        # return-to-the-pin removes it. Declared rather than inherited: a step
        # whose undo happens to be another's still has to say what its own is.
        inverse=LocalInverse(reset_branch_to_pin),
        probe=LocalProbe(probe_snapshot_regenerated),
        # snapshot.json is a committed, tracked file; a reset restores it.
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="BRANCH_PUSHED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=False,
        perform=PlanIssued(performer=_PHASE_A, kinds=(
            RECORD_CANDIDATE,
            GUARD_FOREIGN_COMMITS,
            GUARD_CANDIDATE_WINDOW,
            PUSH_CANDIDATE,
        )),
        inverse=ExternalInverse(
            performed_by="rlsbl release undo",
            condition=(
                "the candidate is on origin's release branch, so withdrawing "
                "it means writing origin. Only `rlsbl release undo` does that, "
                "and only by reverting on top and pushing the branch -- the "
                "candidate commit itself is never removed from origin"
            ),
        ),
        probe=NoLocalProbe(
            "origin's branch head is not a repository input: the "
            "remote-tracking ref records the last fetch, not the remote"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="CI_VERIFIED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=NoInverse(
            "an observation of CI's verdict on the candidate, not a mutation: "
            "there is nothing in the repository to undo"
        ),
        probe=NoLocalProbe(
            "the verdict is a GitHub Actions workflow run's conclusion"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="CHANGELOG_FINALIZED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=LocalInverse(undo_changelog_finalization),
        probe=LocalProbe(probe_changelog_finalized),
        artifacts=changelog_finalization_artifacts,
    ),
    ReleaseStep(
        name="RELEASE_FILE_FINALIZED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=LocalInverse(undo_release_file_finalization),
        probe=LocalProbe(probe_release_file_finalized),
        artifacts=release_file_finalization_artifacts,
    ),
    ReleaseStep(
        name="TAGGED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=LocalInverse(delete_local_tags),
        probe=LocalProbe(probe_tagged),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="PUSHED",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=ExternalInverse(
            performed_by="rlsbl release undo",
            condition=(
                "only while the published tag can still be deleted on origin, "
                "which `rlsbl release undo` does; a tag a consumer has already "
                "resolved is never removed, it is retracted forward"
            ),
        ),
        probe=NoLocalProbe(
            "whether origin carries the tag is a live ls-remote query"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="GITHUB_RELEASE",
        phase=MUTATING_PHASE,
        fatal=True,
        mutates_repository=True,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=ExternalInverse(
            performed_by="rlsbl release undo",
            condition=(
                "the Release is a GitHub object, not a repository one; it is "
                "deleted through the API while it still exists"
            ),
        ),
        probe=NoLocalProbe(
            "a GitHub Release is only observable through the GitHub API"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="SUBTREE_PUBLISHED",
        phase=POST_RELEASE_PHASE,
        fatal=False,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=ExternalInverse(
            performed_by="rlsbl monorepo mirror",
            condition=(
                "the mirror is a derived artifact converged FORWARD from the "
                "source; there is no backward step, and a mirror carrying "
                "foreign commits is refused rather than rewritten"
            ),
        ),
        probe=NoLocalProbe(
            "the mirror's branch head lives in another repository on the "
            "network"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="MIRROR_RELEASED",
        phase=POST_RELEASE_PHASE,
        fatal=False,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=NoInverse(
            "nothing in rlsbl withdraws a mirror's published tag or its "
            "GitHub Release; the mirror is only ever converged forward"
        ),
        probe=NoLocalProbe(
            "the mirror's tags and Releases live in another repository on the "
            "network"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="ASSETS_UPLOADED",
        phase=POST_RELEASE_PHASE,
        fatal=True,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=ExternalInverse(
            performed_by="gh release delete-asset",
            condition=(
                "only while the GitHub Release the assets hang off still "
                "exists; rlsbl itself has no asset-removal surface"
            ),
        ),
        probe=NoLocalProbe(
            "a Release's asset list is only observable through the GitHub API"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="PIPELINES_PUBLISHED",
        phase=POST_RELEASE_PHASE,
        fatal=True,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=ExternalInverse(
            performed_by="rlsbl release yank",
            condition=(
                "a published registry version is never removed, it is "
                "withdrawn forward -- npm deprecate, Go retract, a PyPI manual "
                "checklist -- and only where the registry offers that at all"
            ),
        ),
        probe=NoLocalProbe(
            "whether a version is published is a registry query"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="DEPLOYED",
        phase=POST_RELEASE_PHASE,
        fatal=False,
        mutates_repository=False,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=NoInverse(
            "rlsbl has no rollback deployment: a deployment is superseded by "
            "deploying another version, never reversed"
        ),
        probe=NoLocalProbe(
            "what a deploy target currently runs is a property of that target"
        ),
        artifacts=no_artifacts,
    ),
    ReleaseStep(
        name="POST_HOOKS_RUN",
        phase=POST_RELEASE_PHASE,
        fatal=False,
        mutates_repository=True,
        perform=DirectlyIssued(performer=_MUTATING),
        inverse=NoInverse(
            "the hooks are supplied by the project, so rlsbl cannot know what "
            "they did or how to undo it"
        ),
        probe=NoLocalProbe(
            "a hook's effect is whatever the project's own script did"
        ),
        artifacts=no_artifacts,
    ),
)


# ---------------------------------------------------------------------------
# Derivations -- everything else reads these, never a second copy
# ---------------------------------------------------------------------------


def _validate_table():
    """Refuse a table with duplicate step names or duplicate plan kinds."""
    seen_steps = set()
    seen_kinds = {}
    for step in RELEASE_STEP_TABLE:
        if step.name in seen_steps:
            raise ValueError(f"duplicate release step {step.name!r}")
        seen_steps.add(step.name)
        for kind in step.plan_kinds:
            if kind in seen_kinds:
                raise ValueError(
                    f"plan kind {kind!r} is claimed by both "
                    f"{seen_kinds[kind]!r} and {step.name!r}"
                )
            seen_kinds[kind] = step.name


_validate_table()

STEPS_BY_NAME = {step.name: step for step in RELEASE_STEP_TABLE}


def step_names(phase=None):
    """The recorded step names, in canonical order, optionally one phase."""
    return tuple(
        s.name for s in RELEASE_STEP_TABLE if phase is None or s.phase == phase
    )


def all_plan_kinds():
    """Every Phase-A plan kind the table declares, in step order."""
    return tuple(k for s in RELEASE_STEP_TABLE for k in s.plan_kinds)


def rollback_artifacts(ctx):
    """Every repository path the table says a release creates, in step order.

    The pre-push rollback's orphan sweep: after ``git reset --hard`` returns
    the branch to the pre-release commit, files a finalization created are left
    untracked because they never existed in the pre-release history.
    """
    return tuple(p for s in RELEASE_STEP_TABLE for p in s.artifacts(ctx))
