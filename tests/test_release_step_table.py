"""Tests for the release step table — the one declaration of what each step does.

Three guarantees, and the suite is the only thing that can hold them:

1. Completeness: every step in the table decides its inverse and its probe.
   A step added without deciding fails here rather than silently having
   neither.
2. The recorded names are the state file's format: a state file written by
   the previous code is still understood.
3. Forward then inverse on a real fixture repository returns it to its prior
   state, for every step that declares a local inverse.
"""

import json
import os

import pytest

from githarness import git, init_repo

from rlsbl.changelog.files import finalize_version
from rlsbl.commands.release.release_state import (
    FATAL_STEPS,
    MUTATING_STEPS,
    POST_RELEASE_STEPS,
    RELEASE_STEPS,
    get_missing_steps,
    is_state_complete,
    save_step,
)
from rlsbl.commands.release.rollback import _cleanup_release_artifacts
from rlsbl.commands.release.steps import (
    MUTATING_PHASE,
    POST_RELEASE_PHASE,
    RELEASE_STEP_TABLE,
    STEPS_BY_NAME,
    ArtifactState,
    DirectlyIssued,
    ExternalInverse,
    LocalInverse,
    LocalProbe,
    NoInverse,
    NoLocalProbe,
    PlanIssued,
    ReleaseStep,
    StepContext,
    StepInverseError,
    all_plan_kinds,
    no_artifacts,
    rollback_artifacts,
)
from rlsbl.targets import TARGETS


# The step names as they are recorded in ``in-progress.json``. This literal is
# the state file's FORMAT, not a copy of the table: a release that stalled
# under an older build left a file carrying exactly these strings, and the
# table must still name them or that file becomes unreadable. It changes only
# when a deliberate state-file migration changes it.
RECORDED_STEP_NAMES = (
    "VERSION_BUMPED",
    "COMMITTED",
    "SNAPSHOT_REGENERATED",
    "BRANCH_PUSHED",
    "CI_VERIFIED",
    "CHANGELOG_FINALIZED",
    "RELEASE_FILE_FINALIZED",
    "TAGGED",
    "PUSHED",
    "GITHUB_RELEASE",
    "SUBTREE_PUBLISHED",
    "MIRROR_RELEASED",
    "ASSETS_UPLOADED",
    "PIPELINES_PUBLISHED",
    "DEPLOYED",
    "POST_HOOKS_RUN",
)


def _resolve(dotted):
    """Import ``module:attribute`` and return the attribute."""
    import importlib

    module_name, _, attr = dotted.partition(":")
    return getattr(importlib.import_module(module_name), attr)


# ---------------------------------------------------------------------------
# 1. Completeness -- every step decided
# ---------------------------------------------------------------------------


class TestEveryStepDecides:
    """No step may leave its inverse or its probe unanswered."""

    @pytest.mark.parametrize("step", RELEASE_STEP_TABLE, ids=lambda s: s.name)
    def test_declares_an_inverse_or_says_it_has_none(self, step):
        assert isinstance(
            step.inverse, (LocalInverse, ExternalInverse, NoInverse)
        ), f"{step.name} declares no inverse and does not say it has none"
        if isinstance(step.inverse, LocalInverse):
            assert callable(step.inverse.undo)
        elif isinstance(step.inverse, ExternalInverse):
            # A step that reaches outside the repository states BOTH what
            # performs the undo and the condition under which it is possible.
            assert step.inverse.performed_by.strip()
            assert step.inverse.condition.strip()
        else:
            assert step.inverse.reason.strip()

    @pytest.mark.parametrize("step", RELEASE_STEP_TABLE, ids=lambda s: s.name)
    def test_declares_a_probe_or_says_it_has_no_local_one(self, step):
        assert isinstance(step.probe, (LocalProbe, NoLocalProbe)), (
            f"{step.name} declares no probe and does not say it has no local one"
        )
        if isinstance(step.probe, LocalProbe):
            assert callable(step.probe.observe)
        else:
            assert step.probe.reason.strip()

    @pytest.mark.parametrize("step", RELEASE_STEP_TABLE, ids=lambda s: s.name)
    def test_declares_where_it_is_performed(self, step):
        assert isinstance(step.perform, (PlanIssued, DirectlyIssued))
        # The performer is resolved by import, so the declaration cannot name
        # a callable that was renamed or removed.
        assert callable(_resolve(step.perform.performer))

    @pytest.mark.parametrize("step", RELEASE_STEP_TABLE, ids=lambda s: s.name)
    def test_declares_the_files_it_creates(self, step):
        assert callable(step.artifacts)

    def test_a_step_with_no_inverse_declaration_is_refused(self):
        with pytest.raises(TypeError, match="inverse"):
            ReleaseStep(
                name="MADE_UP", phase=MUTATING_PHASE, fatal=True,
                mutates_repository=True,
                perform=DirectlyIssued(performer="rlsbl.utils:run"),
                inverse=None,
                probe=NoLocalProbe("n/a"),
                artifacts=no_artifacts,
            )

    def test_a_step_with_no_probe_declaration_is_refused(self):
        with pytest.raises(TypeError, match="probe"):
            ReleaseStep(
                name="MADE_UP", phase=MUTATING_PHASE, fatal=True,
                mutates_repository=True,
                perform=DirectlyIssued(performer="rlsbl.utils:run"),
                inverse=NoInverse("n/a"),
                probe=None,
                artifacts=no_artifacts,
            )

    def test_a_step_with_a_bare_callable_inverse_is_refused(self):
        # A bare callable is exactly the "silently absent capability" shape
        # the wrappers exist to prevent: it says nothing about whether the
        # undo is local or reaches outside the repository.
        with pytest.raises(TypeError, match="inverse"):
            ReleaseStep(
                name="MADE_UP", phase=MUTATING_PHASE, fatal=True,
                mutates_repository=True,
                perform=DirectlyIssued(performer="rlsbl.utils:run"),
                inverse=lambda ctx: None,
                probe=NoLocalProbe("n/a"),
                artifacts=no_artifacts,
            )

    def test_an_unknown_phase_is_refused(self):
        with pytest.raises(ValueError, match="phase"):
            ReleaseStep(
                name="MADE_UP", phase="whenever", fatal=True,
                mutates_repository=True,
                perform=DirectlyIssued(performer="rlsbl.utils:run"),
                inverse=NoInverse("n/a"),
                probe=NoLocalProbe("n/a"),
                artifacts=no_artifacts,
            )

    def test_a_plan_issued_step_names_its_kinds(self):
        with pytest.raises(ValueError, match="plan kinds"):
            PlanIssued(performer="rlsbl.utils:run", kinds=())


# ---------------------------------------------------------------------------
# 2. The recorded names are the state file's format
# ---------------------------------------------------------------------------


class TestRecordedNames:
    """A state file written before the table existed is still understood."""

    def test_table_names_match_the_recorded_names_exactly(self):
        assert tuple(s.name for s in RELEASE_STEP_TABLE) == RECORDED_STEP_NAMES

    def test_release_state_projections_derive_from_the_table(self):
        assert RELEASE_STEPS == RECORDED_STEP_NAMES
        assert MUTATING_STEPS == tuple(
            s.name for s in RELEASE_STEP_TABLE if s.phase == MUTATING_PHASE
        )
        assert POST_RELEASE_STEPS == tuple(
            s.name for s in RELEASE_STEP_TABLE if s.phase == POST_RELEASE_PHASE
        )
        assert FATAL_STEPS == frozenset(
            s.name for s in RELEASE_STEP_TABLE if s.fatal
        )

    def test_a_state_file_carrying_the_recorded_names_is_complete(self, tmp_path):
        """Every recorded name is accepted, and the full set reads complete."""
        state_path = str(tmp_path / "in-progress.json")
        for name in RECORDED_STEP_NAMES:
            save_step(state_path, name)
        with open(state_path, encoding="utf-8") as f:
            written = json.load(f)
        assert written["completed_steps"] == list(RECORDED_STEP_NAMES)
        assert get_missing_steps(written) == []
        assert is_state_complete(written)

    def test_a_partial_state_file_names_the_steps_still_owed(self):
        """The shape a stalled release leaves behind still resolves."""
        stalled = {"completed_steps": ["VERSION_BUMPED", "COMMITTED"]}
        missing = get_missing_steps(stalled)
        assert missing == list(RECORDED_STEP_NAMES[2:])
        assert not is_state_complete(stalled)

    def test_every_recorded_name_resolves_in_the_table(self):
        for name in RECORDED_STEP_NAMES:
            assert STEPS_BY_NAME[name].name == name


class TestPlanKindDispatch:
    """The Phase-A executor's dispatch is the table's kinds, not a second list."""

    def test_dispatch_keys_are_exactly_the_declared_kinds(self):
        from rlsbl.commands.release import phase_a

        assert tuple(phase_a._HANDLERS) == all_plan_kinds()

    def test_every_declared_kind_has_an_implementation(self):
        from rlsbl.commands.release import phase_a

        for kind in all_plan_kinds():
            assert callable(phase_a._HANDLERS[kind])

    def test_a_plan_step_naming_an_undeclared_release_step_is_refused(self):
        from rlsbl.commands.release import phase_a

        with pytest.raises(ValueError, match="release step table"):
            phase_a.PlanStep(
                kind=phase_a.COMMIT, release_step="NOT_A_STEP", summary="x",
            )

    def test_a_plan_step_marking_an_undeclared_release_step_is_refused(self):
        from rlsbl.commands.release import phase_a

        with pytest.raises(ValueError, match="release step table"):
            phase_a.PlanStep(
                kind=phase_a.COMMIT, release_step="COMMITTED", summary="x",
                marks=("NOT_A_STEP",),
            )


# ---------------------------------------------------------------------------
# Fixture repository
# ---------------------------------------------------------------------------


def _make_project(tmp_path, *, version="1.0.0"):
    """A minimal committed project: one pypi target, changes and releases dirs."""
    repo = tmp_path / "proj"
    init_repo(repo)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "{version}"\n', encoding="utf-8"
    )
    rlsbl_dir = repo / ".rlsbl"
    (rlsbl_dir / "changes").mkdir(parents=True)
    (rlsbl_dir / "releases").mkdir(parents=True)
    (rlsbl_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n",
        encoding="utf-8",
    )
    (rlsbl_dir / "changes" / "unreleased.jsonl").write_text(
        '{"id": "01", "type": "fix", "description": "a fix", "commits": []}\n',
        encoding="utf-8",
    )
    (rlsbl_dir / "releases" / "unreleased.toml").write_text(
        'bump = "minor"\ndescription = "the fixture release"\ninclude = ["pypi"]\n',
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial")
    return repo


def _context(repo, *, version="1.1.0", previous_version="1.0.0", pin=None,
             tag=None, companion_tags=()):
    return StepContext(
        project_dir=str(repo),
        git_root=str(repo),
        version=version,
        previous_version=previous_version,
        tag=tag,
        branch="main",
        pin_sha=pin,
        changes_dir=str(repo / ".rlsbl" / "changes"),
        releases_dir=str(repo / ".rlsbl" / "releases"),
        workspace_root=None,
        releasable_config_dir=None,
        companion_tags=tuple(companion_tags),
    )


def _tree_snapshot(repo):
    """Every tracked and untracked file's bytes, plus HEAD and the tag list."""
    files = {}
    for root, dirs, names in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names:
            full = os.path.join(root, name)
            files[os.path.relpath(full, repo)] = open(full, "rb").read()
    return {
        "files": files,
        "head": git(repo, "rev-parse", "HEAD"),
        "tags": git(repo, "tag", "-l"),
        "status": git(repo, "status", "--porcelain"),
    }


# ---------------------------------------------------------------------------
# 3. Forward then inverse, per step
# ---------------------------------------------------------------------------


def _forward_version_bumped(repo, ctx):
    TARGETS["pypi"].write_version(str(repo), ctx.version, ctx=None)


def _forward_committed(repo, ctx):
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "{ctx.version}"\n',
        encoding="utf-8",
    )
    git(repo, "add", "pyproject.toml")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m",
        f"chore: release v{ctx.version}")


def _forward_snapshot_regenerated(repo, ctx):
    path = repo / ".rlsbl" / "snapshot.json"
    path.write_text(json.dumps({"packages": {}}) + "\n", encoding="utf-8")
    git(repo, "add", str(path))
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m",
        "chore: regenerate snapshot")


def _forward_changelog_finalized(repo, ctx):
    finalize_version(ctx.changes_dir, ctx.version)
    open(os.path.join(ctx.changes_dir, f"{ctx.version}.md"), "w").write("# notes\n")


def _forward_release_file_finalized(repo, ctx):
    src = os.path.join(ctx.releases_dir, "unreleased.toml")
    dst = os.path.join(ctx.releases_dir, f"v{ctx.version}.toml")
    os.rename(src, dst)
    os.chmod(dst, 0o444)


def _forward_tagged(repo, ctx):
    git(repo, "tag", ctx.tag)
    for companion in ctx.companion_tags:
        git(repo, "tag", companion)


#: One forward driver per step that declares a local inverse. Each performs
#: the step's real effect through the same production code the release uses
#: (or, where the release inlines it, the same git and filesystem operations),
#: so the inverse is exercised against what a release actually leaves behind.
FORWARD_DRIVERS = {
    "VERSION_BUMPED": _forward_version_bumped,
    "COMMITTED": _forward_committed,
    "SNAPSHOT_REGENERATED": _forward_snapshot_regenerated,
    "CHANGELOG_FINALIZED": _forward_changelog_finalized,
    "RELEASE_FILE_FINALIZED": _forward_release_file_finalized,
    "TAGGED": _forward_tagged,
}

_LOCALLY_REVERSIBLE = tuple(
    s.name for s in RELEASE_STEP_TABLE if isinstance(s.inverse, LocalInverse)
)

#: Steps whose artifact only exists in a workspace, so the standalone fixture
#: can never make their probe report PRESENT. Each is probed on a real
#: workspace instead, in the class named as the value.
PROBED_ON_A_WORKSPACE = {
    "SNAPSHOT_REGENERATED":
        "a snapshot only exists in a workspace; probed in TestSnapshotProbe",
}


class TestForwardThenInverse:
    """Every locally reversible step returns the repository to its prior state."""

    def test_every_locally_reversible_step_has_a_forward_driver(self):
        """A new LocalInverse without a round-trip test fails here."""
        assert set(FORWARD_DRIVERS) == set(_LOCALLY_REVERSIBLE)

    @pytest.mark.parametrize("name", _LOCALLY_REVERSIBLE)
    def test_round_trip(self, tmp_path, name):
        repo = _make_project(tmp_path)
        step = STEPS_BY_NAME[name]
        ctx = _context(
            repo, pin=git(repo, "rev-parse", "HEAD"), tag="v1.1.0",
            companion_tags=("fixture/v1.1.0",) if name == "TAGGED" else (),
        )
        before = _tree_snapshot(repo)

        FORWARD_DRIVERS[name](repo, ctx)
        after_forward = _tree_snapshot(repo)
        assert after_forward != before, (
            f"the forward driver for {name} changed nothing"
        )

        step.inverse.undo(ctx)

        # Finalized JSONL comes back read-only-stripped, so compare content
        # rather than mode; everything else must be byte-identical.
        assert _tree_snapshot(repo) == before

    @pytest.mark.parametrize("name", _LOCALLY_REVERSIBLE)
    def test_probe_sees_the_artifact_appear_and_go(self, tmp_path, name):
        step = STEPS_BY_NAME[name]
        if not isinstance(step.probe, LocalProbe):
            pytest.skip(f"{name} declares no local probe")
        if name in PROBED_ON_A_WORKSPACE:
            pytest.skip(PROBED_ON_A_WORKSPACE[name])
        repo = _make_project(tmp_path)
        ctx = _context(
            repo, pin=git(repo, "rev-parse", "HEAD"), tag="v1.1.0",
        )
        assert step.probe.observe(ctx) in (
            ArtifactState.ABSENT, ArtifactState.NOT_APPLICABLE,
        )
        FORWARD_DRIVERS[name](repo, ctx)
        assert step.probe.observe(ctx) is ArtifactState.PRESENT
        step.inverse.undo(ctx)
        assert step.probe.observe(ctx) in (
            ArtifactState.ABSENT, ArtifactState.NOT_APPLICABLE,
        )


class TestInversePreconditions:
    """An inverse refuses rather than guessing when its precondition fails."""

    def test_reset_refuses_without_a_recorded_pin(self, tmp_path):
        repo = _make_project(tmp_path)
        ctx = _context(repo, pin=None)
        with pytest.raises(StepInverseError, match="did not record"):
            STEPS_BY_NAME["COMMITTED"].inverse.undo(ctx)

    def test_reset_refuses_a_pin_that_is_not_an_ancestor(self, tmp_path):
        repo = _make_project(tmp_path)
        other = tmp_path / "other"
        init_repo(other)
        (other / "f.txt").write_text("x", encoding="utf-8")
        git(other, "add", "f.txt")
        git(other, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "unrelated")
        foreign = git(other, "rev-parse", "HEAD")
        # A SHA this repository does not contain: resetting to it would be a
        # guess about a branch that moved outside the release.
        ctx = _context(repo, pin=foreign)
        with pytest.raises(StepInverseError):
            STEPS_BY_NAME["COMMITTED"].inverse.undo(ctx)

    def test_version_inverse_refuses_without_a_previous_version(self, tmp_path):
        repo = _make_project(tmp_path)
        ctx = _context(repo, previous_version=None)
        with pytest.raises(StepInverseError, match="did not record"):
            STEPS_BY_NAME["VERSION_BUMPED"].inverse.undo(ctx)

    def test_tag_inverse_refuses_without_a_tag(self, tmp_path):
        repo = _make_project(tmp_path)
        ctx = _context(repo, tag=None)
        with pytest.raises(StepInverseError, match="did not record"):
            STEPS_BY_NAME["TAGGED"].inverse.undo(ctx)


# ---------------------------------------------------------------------------
# Probes stay local
# ---------------------------------------------------------------------------


class TestProbesAreLocal:
    """A probe reads repository inputs only -- never the network."""

    @pytest.mark.parametrize(
        "step",
        [s for s in RELEASE_STEP_TABLE if isinstance(s.probe, LocalProbe)],
        ids=lambda s: s.name,
    )
    def test_probe_answers_with_the_network_unreachable(self, tmp_path, step,
                                                        monkeypatch):
        """The probe answers without any outbound call.

        Every network door rlsbl has is replaced with one that raises, so a
        probe that reached for the network fails here instead of answering
        from a machine that happens to be online.
        """
        from rlsbl import effects

        def refuse(*args, **kwargs):
            raise AssertionError(f"{step.name}'s probe reached the network")

        monkeypatch.setattr(effects, "urlopen", refuse)
        monkeypatch.setattr(effects, "tcp_connect", refuse)
        monkeypatch.setattr(effects, "gh", refuse)

        repo = _make_project(tmp_path)
        ctx = _context(repo, pin=git(repo, "rev-parse", "HEAD"), tag="v1.1.0")
        assert isinstance(step.probe.observe(ctx), ArtifactState)


# ---------------------------------------------------------------------------
# The rollback sweep reads the table
# ---------------------------------------------------------------------------


class TestRollbackReadsTheTable:
    """The pre-push orphan sweep is the table's artifact declarations."""

    def test_candidates_are_the_declared_artifacts(self, tmp_path):
        repo = _make_project(tmp_path)
        ctx = _context(repo)
        declared = rollback_artifacts(ctx)
        assert declared == (
            os.path.join(ctx.changes_dir, "1.1.0.jsonl"),
            os.path.join(ctx.changes_dir, "1.1.0.md"),
            os.path.join(ctx.releases_dir, "v1.1.0.toml"),
            os.path.join(ctx.releases_dir, "v1.1.0.md"),
        )

    def test_sweep_removes_every_declared_orphan(self, tmp_path):
        repo = _make_project(tmp_path)
        ctx = _context(repo)
        for path in rollback_artifacts(ctx):
            with open(path, "w", encoding="utf-8") as f:
                f.write("orphan\n")
        _cleanup_release_artifacts(
            str(repo), "1.1.0",
            changes_dir=ctx.changes_dir, releases_dir=ctx.releases_dir,
        )
        for path in rollback_artifacts(ctx):
            assert not os.path.exists(path), f"{path} was left behind"


class TestSnapshotProbe:
    """The snapshot probe answers from the workspace it describes, locally."""

    def _workspace(self, root):
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        projects = load_workspace(str(root))
        return projects, WorkspaceGraph(str(root), projects)

    def _ctx(self, root):
        return StepContext(
            project_dir=str(root), git_root=str(root),
            version="1.1.0", previous_version="1.0.0",
            tag=None, branch="main", pin_sha=None,
            changes_dir=None, releases_dir=None,
            workspace_root=str(root), releasable_config_dir=None,
        )

    def test_absent_then_present(self, monorepo_fixture):
        from rlsbl.snapshot import generate_snapshot, write_snapshot

        root = monorepo_fixture.root
        step = STEPS_BY_NAME["SNAPSHOT_REGENERATED"]
        ctx = self._ctx(root)
        assert step.probe.observe(ctx) is ArtifactState.ABSENT

        projects, graph = self._workspace(root)
        write_snapshot(str(root), generate_snapshot(str(root), projects, graph))
        assert step.probe.observe(ctx) is ArtifactState.PRESENT

    def test_a_stale_snapshot_reads_absent(self, monorepo_fixture):
        """The artifact "exists" only while it matches what it describes."""
        from rlsbl.snapshot import SNAPSHOT_FILE, generate_snapshot, write_snapshot
        from rlsbl.workspace import WORKSPACE_DIR

        root = monorepo_fixture.root
        step = STEPS_BY_NAME["SNAPSHOT_REGENERATED"]
        projects, graph = self._workspace(root)
        write_snapshot(str(root), generate_snapshot(str(root), projects, graph))

        path = os.path.join(str(root), WORKSPACE_DIR, SNAPSHOT_FILE)
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["packages"] = {}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        assert step.probe.observe(self._ctx(root)) is ArtifactState.ABSENT
