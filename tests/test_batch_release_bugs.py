"""Tests for batch release bugs: redundant validation, dirty tree sources, and release init warning."""

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
        (rlsbl_dir / "config.json").write_text('{"private": true}')
        return ProjectContext(
            project_root=project_root,
            workspace_root=None,
            config={"private": True},
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


def _simulate_release(ctx, released_sink=None):
    """Reproduce a real release's observable effects from the resolved plan."""
    name = os.path.basename(str(ctx.project_root))
    ws = str(ctx.workspace_root)
    plan = read_batch_plan(get_batch_plan_path(ws))
    item = plan.items[name]
    _set_pyproject_version(os.path.join(ws, name), item.target_version)
    _git_tag(ws, item.tag)
    if released_sink is not None:
        released_sink.append(name)


def _run_batch(ws, quiet=False):
    _cmd_batch_release(
        {"dry-run": False, "yes": True, "quiet": quiet}, project_root=ws
    )


@pytest.fixture
def _no_commit_noise():
    """Silence the plan/archive commits (real safegit/git) during batch tests."""
    with patch("rlsbl.commands.monorepo.batch_release.commit_files"):
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

    def test_all_completed_out_of_band_archives_without_releasing(
        self, mock_git_repo, capsys, bypass_upfront_validation, _no_commit_noise
    ):
        """(b) Everything already released per an existing plan: the next batch
        invocation archives both files and releases nothing."""
        ws = mock_git_repo
        _setup_batch_packages(ws, ["alpha", "beta"])

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
            _run_batch(ws)

        assert released == []  # nothing released
        releases_dir = os.path.dirname(get_batch_release_file_path(str(ws)))
        names = os.listdir(releases_dir)
        assert "unreleased.toml" not in names
        assert "unreleased.plan.json" not in names
        assert any(n.startswith("batch-") and n.endswith(".toml") for n in names)
        assert any(n.startswith("batch-") and n.endswith(".plan.json") for n in names)

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
