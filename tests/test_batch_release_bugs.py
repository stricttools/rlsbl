"""Tests for batch release bugs: redundant validation, dirty tree sources, and release init warning."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from rlsbl.commands.release import _run_cmd_inner
from rlsbl.commands.release_init import run_cmd as release_init_run_cmd
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig, get_batch_release_file_path
from rlsbl.workspace import WORKSPACE_DIR, WORKSPACE_FILE, save_workspace
from rlsbl.commands.monorepo.batch_plan import (
    BatchPlan,
    PlanItem,
    get_batch_plan_path,
    read_batch_plan,
    write_batch_plan,
)
from rlsbl.commands.monorepo.batch_release import _cmd_batch_release


# ---------------------------------------------------------------------------
# Bug 1: _run_cmd_inner should skip environment validation in batch mode
# ---------------------------------------------------------------------------


class TestBatchModeSkipsValidation:
    """validate_gh_cli, validate_clean_tree, and validate_branch_and_remote
    should NOT be called when batch-mode is True, because the batch
    orchestrator already validated them upfront."""

    def _make_ctx(self, tmp_path):
        """Create a minimal ProjectContext for testing."""
        project_root = tmp_path / "proj"
        project_root.mkdir()
        # Need .rlsbl/config.json for the release flow
        rlsbl_dir = project_root / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"publish_mode": "none"}')
        return ProjectContext(
            project_root=project_root,
            workspace_root=None,
            config={"publish_mode": "none"},
        )

    def _make_release_config(self):
        """Create a minimal ReleaseConfig."""
        return ReleaseConfig(
            bump="patch",
            description="test release",
            include=["pypi"],
            exclude=[],
        )

    @patch("rlsbl.commands.release.validate_branch_and_remote")
    @patch("rlsbl.commands.release.validate_clean_tree")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets")
    def test_batch_mode_skips_env_validation(
        self,
        mock_validate_targets,
        mock_validate_ota,
        mock_validate_config,
        mock_validate_pipeline,
        mock_validate_gh,
        mock_validate_clean,
        mock_validate_branch,
        tmp_path,
    ):
        """When batch-mode=True, the three environment validators must not be called."""
        ctx = self._make_ctx(tmp_path)
        rc = self._make_release_config()
        flags = {"batch-mode": True, "quiet": True}

        # We expect _run_cmd_inner to proceed past validation and fail somewhere
        # later (e.g., resolving monorepo context or computing version).
        # That's fine -- we only care that the three validators are NOT called.
        mock_validate_targets.return_value = set()

        with pytest.raises(Exception):
            # Will fail at some later point; we just need to verify the mocks
            _run_cmd_inner(rc, flags, ctx=ctx)

        mock_validate_gh.assert_not_called()
        mock_validate_clean.assert_not_called()
        mock_validate_branch.assert_not_called()

    @patch("rlsbl.commands.release.validate_branch_and_remote")
    @patch("rlsbl.commands.release.validate_clean_tree")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets")
    def test_non_batch_mode_calls_env_validation(
        self,
        mock_validate_targets,
        mock_validate_ota,
        mock_validate_config,
        mock_validate_pipeline,
        mock_validate_gh,
        mock_validate_clean,
        mock_validate_branch,
        tmp_path,
    ):
        """When batch-mode is not set, the three environment validators MUST be called."""
        ctx = self._make_ctx(tmp_path)
        rc = self._make_release_config()
        flags = {"quiet": True}

        mock_validate_targets.return_value = set()
        mock_validate_clean.return_value = set()
        mock_validate_branch.return_value = "main"

        with pytest.raises(Exception):
            _run_cmd_inner(rc, flags, ctx=ctx)

        mock_validate_gh.assert_called_once()
        mock_validate_clean.assert_called_once()
        mock_validate_branch.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 1c: release init warns in explicit-mode monorepo
# ---------------------------------------------------------------------------


def _setup_explicit_workspace(tmp_path):
    """Set up a monorepo workspace with [[releasables]] and a pypi project."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    ws_toml = ws_dir / WORKSPACE_FILE
    ws_toml.write_text(
        '[[projects]]\npath = "pkg-a"\n\n'
        '[[releasables]]\nname = "core"\nmembers = ["pkg-a"]\n'
    )
    # Create the project directory with a detectable target
    proj_dir = tmp_path / "pkg-a"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
    )
    return proj_dir


def _setup_implicit_workspace(tmp_path):
    """Set up a monorepo workspace WITHOUT [[releasables]] (implicit mode)."""
    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    ws_toml = ws_dir / WORKSPACE_FILE
    ws_toml.write_text('[[projects]]\npath = "pkg-a"\n')
    proj_dir = tmp_path / "pkg-a"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\n'
    )
    return proj_dir


class TestReleaseInitExplicitModeWarning:
    """rlsbl release init should warn when run inside an explicit-mode monorepo."""

    def test_warns_in_explicit_mode(self, tmp_path, capsys):
        """release init emits a warning when workspace uses [[releasables]]."""
        proj_dir = _setup_explicit_workspace(tmp_path)
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "rlsbl monorepo release init" in captured.err
        assert "explicit mode" in captured.err

    def test_no_warning_in_implicit_mode(self, tmp_path, capsys):
        """release init does NOT warn when workspace is in implicit mode."""
        proj_dir = _setup_implicit_workspace(tmp_path)
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "explicit mode" not in captured.err

    def test_no_warning_outside_monorepo(self, tmp_path, capsys):
        """release init does NOT warn when not inside a monorepo."""
        proj_dir = tmp_path / "standalone"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "standalone"\nversion = "0.1.0"\n'
        )
        release_init_run_cmd(proj_dir)
        captured = capsys.readouterr()
        assert "explicit mode" not in captured.err


# ---------------------------------------------------------------------------
# Phase 1.2: resolved-plan sidecar + per-item idempotency + archive-as-repair
#
# These tests simulate batch releases against a real git repo. run_cmd is
# mocked to reproduce the *observable* effects of a real release: it bumps the
# package manifest to the plan's target_version and creates the plan's git tag.
# That lets the skip predicate (live version == target AND tag exists) fire.
# ---------------------------------------------------------------------------


def _write_pyproject(proj_dir, name, version):
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(f'[project]\nname = "{name}"\nversion = "{version}"\n')


def _set_pyproject_version(proj_dir, version):
    path = os.path.join(proj_dir, "pyproject.toml")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            if line.startswith("version"):
                f.write(f'version = "{version}"\n')
            else:
                f.write(line)


def _git_tag(ws, tag):
    subprocess.run(["git", "tag", tag], cwd=str(ws), check=True)


def _setup_batch_packages(ws, names, base_version="0.1.0", pretag=True):
    """Create an implicit-mode workspace with pypi packages and a batch file.

    When ``pretag`` is set, each package's base tag (``name@vBASE``) is created
    so compute_release_version takes the bump branch (real version change).
    Returns the batch file path.
    """
    projects = [{"path": n, "name": n} for n in names]
    save_workspace(str(ws), projects)
    for n in names:
        _write_pyproject(os.path.join(str(ws), n), n, base_version)
        if pretag:
            _git_tag(ws, f"{n}@v{base_version}")

    batch_path = get_batch_release_file_path(str(ws))
    os.makedirs(os.path.dirname(batch_path), exist_ok=True)
    sections = []
    for n in names:
        sections.append(
            f'[packages.{n}]\n'
            f'bump = "patch"\ndescription = "release {n}"\n'
            f'include = ["pypi"]\nexclude = []\n'
        )
    with open(batch_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sections))
    return batch_path


def _simulate_release_for(ws, name):
    """Reproduce a real release's observable effects from the resolved plan."""
    plan = read_batch_plan(get_batch_plan_path(str(ws)))
    item = plan.items[name]
    _set_pyproject_version(os.path.join(str(ws), name), item.target_version)
    _git_tag(ws, item.tag)


def _simulate_release(ctx, released_sink=None):
    """``_simulate_release_for`` addressed by the member's ProjectContext."""
    name = os.path.basename(str(ctx.project_root))
    _simulate_release_for(str(ctx.workspace_root), name)
    if released_sink is not None:
        released_sink.append(name)


def _run_batch(ws, quiet=False):
    _cmd_batch_release(
        {"dry-run": False, "quiet": quiet}, project_root=ws
    )


@pytest.fixture
def _no_commit_noise():
    """Silence the plan/archive commits (real safegit/git) during batch tests
    and model the remote as mirroring locally-created tags.

    ``item_is_released`` now also requires the tag to exist on the remote
    (``tag_exists_on_remote``). The test git repos have no reachable origin, so
    the live ``git ls-remote`` would raise. A released item's tag is pushed in
    reality, so we mirror the remote off the local tag set: a locally-tagged
    item counts as pushed, an un-tagged (e.g. interrupted) one does not.
    """
    from rlsbl.utils import tag_exists_locally

    with (
        patch("rlsbl.commands.monorepo.batch_release.commit_files"),
        patch(
            "rlsbl.commands.monorepo.batch_plan.tag_exists_on_remote",
            side_effect=lambda tag, cwd=None: tag_exists_locally(tag, cwd=cwd),
        ),
    ):
        yield


class TestBatchPlanIdempotency:

    def test_mid_loop_interruption_reruns_skip_completed(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(a) Interrupt mid-batch, rerun: completed items are SKIPPED, the
        remainder released, and the batch file + plan are archived."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta", "gamma"])

        # First run: alpha releases, beta raises (interruption), gamma untouched.
        def mock_first(release_config, flags, **kwargs):
            name = os.path.basename(str(kwargs["ctx"].project_root))
            if name == "beta":
                raise SystemExit(1)
            _simulate_release(kwargs["ctx"])

        with patch("rlsbl.commands.release.run_cmd", mock_first):
            with pytest.raises(SystemExit):
                _run_batch(ws)

        # Plan was persisted before any release; alpha is at 0.1.1 with its tag.
        plan = read_batch_plan(get_batch_plan_path(str(ws)))
        assert plan.items["alpha"].base_version == "0.1.0"
        assert plan.items["alpha"].target_version == "0.1.1"
        assert plan.items["alpha"].tag == "alpha@v0.1.1"
        capsys.readouterr()  # clear captured output

        # Second run: alpha skipped, beta + gamma released.
        released = []

        def mock_second(release_config, flags, **kwargs):
            _simulate_release(kwargs["ctx"], released_sink=released)

        with patch("rlsbl.commands.release.run_cmd", mock_second):
            _run_batch(ws)

        out = capsys.readouterr().out
        assert "Skipping alpha" in out
        assert released == ["beta", "gamma"]

        # Both files archived; no stale unreleased files remain.
        releases_dir = os.path.dirname(get_batch_release_file_path(str(ws)))
        names = os.listdir(releases_dir)
        assert "unreleased.toml" not in names
        assert "unreleased.plan.json" not in names
        assert any(n.startswith("batch-") and n.endswith(".toml") for n in names)
        assert any(n.startswith("batch-") and n.endswith(".plan.json") for n in names)

    def test_completed_plan_exits_nonzero_and_archives_only_the_sidecar(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(b) Everything already released per an existing plan.

        This used to archive BOTH files and exit 0 -- a silent success for a
        run that released nothing. The plan sidecar is stale and gets
        archived (that is what unblocks the next run); the batch file is
        never touched, and the run exits nonzero naming the stale plan.
        """
        ws = mock_git_repo
        batch_path = _setup_batch_packages(ws, ["alpha", "beta"])

        # Construct the released end-state directly: manifests at target,
        # target tags created, and a matching plan persisted -- but the
        # unreleased.toml is still on disk (the stale-file scenario).
        plan = BatchPlan(
            section_type="packages",
            items={
                "alpha": PlanItem("alpha", "0.1.0", "0.1.1", "alpha@v0.1.1", "pypi", "patch"),
                "beta": PlanItem("beta", "0.1.0", "0.1.1", "beta@v0.1.1", "pypi", "patch"),
            },
        )
        write_batch_plan(get_batch_plan_path(str(ws)), plan)
        for n in ["alpha", "beta"]:
            _set_pyproject_version(os.path.join(str(ws), n), "0.1.1")
            _git_tag(ws, f"{n}@v0.1.1")

        released = []

        def mock_run(release_config, flags, **kwargs):
            released.append(os.path.basename(str(kwargs["ctx"].project_root)))

        with patch("rlsbl.commands.release.run_cmd", mock_run):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)

        assert exc.value.code == 1
        assert released == []  # nothing released
        releases_dir = os.path.dirname(batch_path)
        names = os.listdir(releases_dir)
        assert "unreleased.toml" in names, (
            "the batch file must never be consumed by the already-complete path"
        )
        assert not any(n.startswith("batch-") and n.endswith(".toml") for n in names)
        assert "unreleased.plan.json" not in names, (
            "the stale plan must be archived so a re-run can resolve a fresh one"
        )
        assert any(n.startswith("batch-") and n.endswith(".plan.json") for n in names)

        err = capsys.readouterr().err
        assert "unreleased.plan.json" in err
        assert "unreleased.toml" in err

    def test_completed_plan_does_not_consume_a_fresh_release_file(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """The operator wrote a fresh batch file for the NEXT release while a
        completed plan sidecar was still on disk.

        The plan is matched against the batch file by item set and bump
        intent only, so the fresh file validates against the stale plan --
        and the old already-complete path renamed it into the archive without
        releasing anything. The file must survive byte-for-byte, and clearing
        the stale plan must be enough for a re-run to release properly.
        """
        ws = mock_git_repo
        batch_path = _setup_batch_packages(ws, ["alpha"])

        plan = BatchPlan(
            section_type="packages",
            items={
                "alpha": PlanItem("alpha", "0.1.0", "0.1.1", "alpha@v0.1.1", "pypi", "patch"),
            },
        )
        write_batch_plan(get_batch_plan_path(str(ws)), plan)
        _set_pyproject_version(os.path.join(str(ws), "alpha"), "0.1.1")
        _git_tag(ws, "alpha@v0.1.1")

        with open(batch_path, "r", encoding="utf-8") as f:
            fresh_contents = f.read()

        def refuse(release_config, flags, **kwargs):
            raise AssertionError("nothing to release against a completed plan")

        with patch("rlsbl.commands.release.run_cmd", refuse):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)
        assert exc.value.code == 1

        with open(batch_path, "r", encoding="utf-8") as f:
            assert f.read() == fresh_contents, (
                "the operator's fresh batch file must be untouched"
            )

        # The remedy works: with the stale plan gone, a re-run resolves a
        # fresh plan against the surviving batch file and releases for real.
        capsys.readouterr()
        released = []

        def mock_run(release_config, flags, **kwargs):
            _simulate_release(kwargs["ctx"], released_sink=released)

        with patch("rlsbl.commands.release.run_cmd", mock_run):
            _run_batch(ws)

        assert released == ["alpha"]
        plan_after = read_batch_plan(
            os.path.join(os.path.dirname(batch_path), "unreleased.plan.json")
        ) if os.path.exists(
            os.path.join(os.path.dirname(batch_path), "unreleased.plan.json")
        ) else None
        assert plan_after is None, "the completed batch archives its own plan"

    def test_early_pass_does_not_archive_when_item_in_progress(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """The early repair pass must NOT archive the batch when a plan item
        still has an on-disk in-progress.json, even if its tag exists (a failed
        push can leave the tag locally while the release did not complete). The
        batch file must survive so the stranded release can be resumed.
        """
        from rlsbl.commands.release.release_state import get_state_path

        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])

        # Both items appear released per plan (manifests + tags), but beta still
        # has a lingering in-progress.json -- an unfinished/resumable release.
        plan = BatchPlan(
            section_type="packages",
            items={
                "alpha": PlanItem("alpha", "0.1.0", "0.1.1", "alpha@v0.1.1", "pypi", "patch"),
                "beta": PlanItem("beta", "0.1.0", "0.1.1", "beta@v0.1.1", "pypi", "patch"),
            },
        )
        write_batch_plan(get_batch_plan_path(str(ws)), plan)
        for n in ["alpha", "beta"]:
            _set_pyproject_version(os.path.join(str(ws), n), "0.1.1")
            _git_tag(ws, f"{n}@v0.1.1")

        # Strand beta with an in-progress.json.
        beta_state = get_state_path(os.path.join(str(ws), "beta"))
        os.makedirs(os.path.dirname(beta_state), exist_ok=True)
        with open(beta_state, "w", encoding="utf-8") as f:
            f.write('{"new_version": "0.1.1", "tag": "beta@v0.1.1"}\n')

        # Both items are already released (tags present), so the fall-through
        # flow skips them without releasing; run_cmd must never be called.
        def mock_run(release_config, flags, **kwargs):
            raise AssertionError("must not release while an item is stranded")

        with patch("rlsbl.commands.release.run_cmd", mock_run), \
             patch("rlsbl.commands.monorepo.batch_release.validate_gh_push_access"):
            # Nothing can be released while beta is stranded, so the run has
            # no completion to report: it exits nonzero rather than printing
            # "Batch release complete" over an empty released list.
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)
        assert exc.value.code == 1

        # Neither file may be archived while beta is stranded: the batch file
        # is the operator's, and the plan is what a resume still needs.
        releases_dir = os.path.dirname(get_batch_release_file_path(str(ws)))
        names = os.listdir(releases_dir)
        assert "unreleased.toml" in names, "must not archive a stranded batch"
        assert "unreleased.plan.json" in names, (
            "the plan a stranded item still needs must not be archived"
        )
        assert not any(
            n.startswith("batch-") and n.endswith(".toml") for n in names
        ), "no archived batch file should exist"
        assert not any(
            n.startswith("batch-") and n.endswith(".plan.json") for n in names
        ), "no archived plan file should exist"
        assert "beta" in capsys.readouterr().err

    def test_happy_path_tail_archives_batch_and_plan(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(c) A clean full run archives the batch file AND the plan sidecar
        at the loop tail."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])

        released = []

        def mock_run(release_config, flags, **kwargs):
            _simulate_release(kwargs["ctx"], released_sink=released)

        with patch("rlsbl.commands.release.run_cmd", mock_run):
            _run_batch(ws)

        assert released == ["alpha", "beta"]
        releases_dir = os.path.dirname(get_batch_release_file_path(str(ws)))
        names = os.listdir(releases_dir)
        assert "unreleased.toml" not in names
        assert "unreleased.plan.json" not in names
        toml_archives = [n for n in names if n.startswith("batch-") and n.endswith(".toml")]
        plan_archives = [n for n in names if n.startswith("batch-") and n.endswith(".plan.json")]
        assert len(toml_archives) == 1
        assert len(plan_archives) == 1
        # Same timestamped stem for both.
        assert toml_archives[0][:-len(".toml")] == plan_archives[0][:-len(".plan.json")]

    def test_legacy_plan_less_partial_batch_hard_errors(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(d) A batch file with an already-existing target tag and NO plan
        sidecar predates plan tracking -> hard error naming both files."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha"])
        # The batch's target tag already exists but there is no plan sidecar.
        _git_tag(ws, "alpha@v0.1.1")
        assert not os.path.exists(get_batch_plan_path(str(ws)))

        def mock_run(release_config, flags, **kwargs):
            raise AssertionError("run_cmd must not be called for a legacy batch")

        with patch("rlsbl.commands.release.run_cmd", mock_run):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "predates resolved-plan tracking" in err
        assert "unreleased.toml" in err
        assert "unreleased.plan.json" in err

    def test_existing_plan_is_reused_not_regenerated(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(e) On rerun, the persisted plan is reused: base versions reflect the
        original plan-time state, not the drifted (already-bumped) live state."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])

        # First run releases alpha, then interrupts on beta.
        def mock_first(release_config, flags, **kwargs):
            name = os.path.basename(str(kwargs["ctx"].project_root))
            if name == "beta":
                raise SystemExit(1)
            _simulate_release(kwargs["ctx"])

        with patch("rlsbl.commands.release.run_cmd", mock_first):
            with pytest.raises(SystemExit):
                _run_batch(ws)

        # alpha's live version is now 0.1.1, but the plan must still record the
        # pre-batch base of 0.1.0 (never recomputed from drifted state).
        plan_before = read_batch_plan(get_batch_plan_path(str(ws)))
        alpha_base_before = plan_before.items["alpha"].base_version
        alpha_target_before = plan_before.items["alpha"].target_version

        def mock_second(release_config, flags, **kwargs):
            _simulate_release(kwargs["ctx"])

        with patch("rlsbl.commands.release.run_cmd", mock_second):
            _run_batch(ws)

        assert alpha_base_before == "0.1.0"
        assert alpha_target_before == "0.1.1"
        # If the plan had been regenerated on rerun, alpha's base would have
        # drifted to 0.1.1 and target to 0.1.2. It must not.
        assert plan_before.items["alpha"].target_version == "0.1.1"


# ---------------------------------------------------------------------------
# Phase 3.1(c): a batch release never reports success it cannot evidence.
#
# Two silent-zero holes lived here. A member whose release call exited 0
# skipped the batch's own bookkeeping entirely -- neither `released` nor
# `pending` learned about it -- and the batch moved on. And the completion
# line printed unconditionally, so a run that released nothing still ended
# with "Batch release complete: " and exit 0.
# ---------------------------------------------------------------------------


class TestBatchMemberSilentExitZero:

    def test_member_exit_zero_without_state_is_a_hard_error(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """A member that exits 0 while leaving no in-progress state gives the
        batch no evidence it released. That must abort, not be skipped."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha"])

        def mock_run(release_config, flags, **kwargs):
            raise SystemExit(0)

        with patch("rlsbl.commands.release.run_cmd", mock_run):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)

        assert exc.value.code == 1
        out = capsys.readouterr()
        assert "alpha" in out.err
        assert "Batch release complete" not in out.out, (
            "a batch that released nothing must not announce completion"
        )

    def test_member_exit_zero_with_state_is_treated_as_pending(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """A member that exits 0 but DID leave in-progress state is genuinely
        mid-flight: pass 2 finishes it on the verified candidate."""
        from rlsbl.commands.release.release_state import get_state_path

        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha"])

        def mock_run(release_config, flags, **kwargs):
            project_dir = str(kwargs["ctx"].project_root)
            state_path = get_state_path(project_dir)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                f.write('{"new_version": "0.1.1", "tag": "alpha@v0.1.1"}\n')
            raise SystemExit(0)

        completed = []

        def fake_resume(name, project_dir, workspace_root, state_path, flags,
                        verified_sha, log):
            completed.append(name)
            _simulate_release_for(ws, name)
            os.remove(state_path)  # a real resume clears its state on success

        with (
            patch("rlsbl.commands.release.run_cmd", mock_run),
            patch("rlsbl.commands.monorepo.batch_release._publish_batch_candidate",
                  return_value="deadbeef"),
            patch("rlsbl.commands.monorepo.batch_release._batch_ci_gate",
                  return_value="deadbeef"),
            patch("rlsbl.commands.monorepo.batch_release._resume_batch_item",
                  side_effect=fake_resume),
        ):
            _run_batch(ws)

        assert completed == ["alpha"], (
            "a member with in-progress state must reach pass 2, not be dropped"
        )
        assert "Batch release complete: alpha" in capsys.readouterr().out


class TestBatchCompletionRequiresEvidence:

    def test_batch_that_released_nothing_exits_nonzero(
        self, mock_git_repo, capsys, bypass_upfront_validation,
    ):
        """Every item skipped as already-released and nothing released: the
        run announced completion and exited 0.

        Reproduced through the real divergence that reaches this state: the
        repair pass's remote-tag probe blips (``item_is_released`` reads an
        inconclusive ls-remote as not-released), so the batch falls through,
        and by the time the loop re-probes, every item reads as released.
        """
        from rlsbl.utils import tag_exists_locally

        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])

        plan = BatchPlan(
            section_type="packages",
            items={
                "alpha": PlanItem("alpha", "0.1.0", "0.1.1", "alpha@v0.1.1", "pypi", "patch"),
                "beta": PlanItem("beta", "0.1.0", "0.1.1", "beta@v0.1.1", "pypi", "patch"),
            },
        )
        write_batch_plan(get_batch_plan_path(str(ws)), plan)
        for n in ["alpha", "beta"]:
            _set_pyproject_version(os.path.join(str(ws), n), "0.1.1")
            _git_tag(ws, f"{n}@v0.1.1")

        calls = {"n": 0}

        def blip_once(tag, cwd=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-remote"])
            return tag_exists_locally(tag, cwd=cwd)

        def mock_run(release_config, flags, **kwargs):
            raise AssertionError("nothing should be released here")

        with (
            patch("rlsbl.commands.monorepo.batch_release.commit_files"),
            patch("rlsbl.commands.monorepo.batch_plan.tag_exists_on_remote",
                  side_effect=blip_once),
            patch("rlsbl.commands.release.run_cmd", mock_run),
        ):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)

        assert exc.value.code == 1
        out = capsys.readouterr()
        assert "Batch release complete" not in out.out
        assert "released nothing" in out.err


# ---------------------------------------------------------------------------
# Batch resume seeding: a member stranded by a PREVIOUS invocation
# ---------------------------------------------------------------------------


class TestBatchSeedsStrandedMembers:
    """A batch that crashed between pass 1 and pass 2 leaves members holding
    an in-progress.json: committed locally, never pushed, never gated, never
    tagged. Re-running the batch used to hand each of them straight back to
    ``release run``, which refuses point-blank ("a previous release is in
    progress") -- so the batch died on the very state IT had created, and the
    only way forward was to resume every member by hand, one at a time,
    outside the batch. That defeats the whole reason the batch exists: ONE
    push, ONE CI gate for the group.

    Pass 1 now seeds ``pending`` from the plan's in-progress items and skips
    their pass-1 release call, so they flow through the existing
    candidate-push -> CI-gate -> resume sequence with everyone else.
    """

    def _stranded_plan(self, ws, names):
        plan = BatchPlan(
            section_type="packages",
            items={
                n: PlanItem(n, "0.1.0", "0.1.1", f"{n}@v0.1.1", "pypi", "patch")
                for n in names
            },
        )
        write_batch_plan(get_batch_plan_path(str(ws)), plan)
        return plan

    def _strand(self, ws, name, completed_steps):
        from rlsbl.commands.release.release_state import get_state_path

        state_path = get_state_path(os.path.join(str(ws), name))
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({
                "new_version": "0.1.1",
                "tag": f"{name}@v0.1.1",
                "branch": "main",
                "registry": "pypi",
                "completed_steps": completed_steps,
                "release_commits": [],
            }, f)
        return state_path

    def _patched_batch(self, ws, run_cmd_impl):
        """Run the batch with the push/gate/resume trio recorded, not executed."""
        calls = {"released": [], "resumed": []}

        def fake_run_cmd(release_config, flags, **kwargs):
            calls["released"].append(
                os.path.basename(str(kwargs["ctx"].project_root))
            )
            run_cmd_impl(release_config, flags, **kwargs)

        def fake_resume(name, project_dir, workspace_root, state_path, flags,
                        verified_sha, log):
            calls["resumed"].append((name, verified_sha))

        with (
            patch("rlsbl.commands.release.run_cmd", fake_run_cmd),
            patch(
                "rlsbl.commands.monorepo.batch_release._publish_batch_candidate",
                return_value="cafebabe" * 5,
            ),
            patch(
                "rlsbl.commands.monorepo.batch_release._batch_ci_gate",
                side_effect=lambda ws_root, flags, log, sha, pending: sha,
            ),
            patch(
                "rlsbl.commands.monorepo.batch_release._resume_batch_item",
                fake_resume,
            ),
        ):
            _run_batch(ws, quiet=False)
        return calls

    def test_stranded_member_is_resumed_inside_the_batch(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise,
    ):
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])
        self._stranded_plan(ws, ["alpha", "beta"])
        self._strand(ws, "alpha", ["VERSION_BUMPED", "COMMITTED"])

        calls = self._patched_batch(ws, lambda rc, fl, **kw: _simulate_release(kw["ctx"]))

        assert calls["released"] == ["beta"], (
            "the stranded member must NOT be handed back to `release run`"
        )
        assert [name for name, _sha in calls["resumed"]] == ["alpha"]
        assert calls["resumed"][0][1] == "cafebabe" * 5, (
            "the stranded member finishes on the batch's verified candidate"
        )
        assert "alpha" in capsys.readouterr().out

    def test_a_member_past_the_ci_gate_is_refused_by_name(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise,
    ):
        """A member sealed to a CI-verified commit must not be re-gated.

        Past CI_VERIFIED the release is committed to the exact commit CI
        judged; folding it into a NEW batch candidate would move the commit
        its tag and its CI-SHA marker address. The batch refuses and names the
        remedy instead of guessing.
        """
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])
        self._stranded_plan(ws, ["alpha", "beta"])
        self._strand(
            ws, "alpha",
            ["VERSION_BUMPED", "COMMITTED", "BRANCH_PUSHED", "CI_VERIFIED"],
        )

        def refuse(release_config, flags, **kwargs):
            raise AssertionError("nothing may be released past a sealed member")

        with patch("rlsbl.commands.release.run_cmd", refuse):
            with pytest.raises(SystemExit) as exc:
                _run_batch(ws)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "alpha" in err
        assert "rlsbl release resume" in err
