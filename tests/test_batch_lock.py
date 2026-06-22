"""Tests for batch monorepo-wide lock (Phase 1).

Verifies:
- _run_cmd_inner with skip-lock=True skips acquire_lock/release_lock
- _run_cmd_inner without skip-lock calls acquire_lock/release_lock normally
- Batch release passes skip-lock=True to inner releases
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from conftest import make_ctx, make_workspace, run_git
from rlsbl.release_file import BatchReleaseConfig, ReleaseConfig
from rlsbl.workspace import WORKSPACE_DIR


# ---------------------------------------------------------------------------
# 1. skip-lock flag in _run_cmd_inner
# ---------------------------------------------------------------------------


def _make_release_context(tmp_path):
    """Build a minimal project context for _run_cmd_inner tests."""
    rlsbl_dir = tmp_path / ".rlsbl"
    rlsbl_dir.mkdir()
    (rlsbl_dir / "config.json").write_text(json.dumps({"private": False}))
    return make_ctx(tmp_path, config={"private": False})


def _make_release_config():
    """Create a minimal ReleaseConfig."""
    return ReleaseConfig(bump="patch", include=[], exclude=[], description="test release")


class TestSkipLockInRunCmdInner:
    """Test that _run_cmd_inner respects skip-lock flag for acquire/release."""

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release._run_release_mutating")
    @patch("rlsbl.commands.release.resolve_release_targets", return_value=[])
    @patch("rlsbl.commands.release.print_dry_run_summary")
    @patch("rlsbl.commands.release.generate_changelog", return_value="# Changelog\n\n## 0.1.1\n\n- test\n")
    @patch("rlsbl.commands.release.validate_changelog_state", return_value=None)
    @patch("rlsbl.commands.release.validate_blog_body", return_value=(None, None))
    @patch("rlsbl.commands.release.resolve_monorepo_context", return_value=(None, None, False, False, None))
    @patch("rlsbl.commands.release.validate_branch_and_remote", return_value="main")
    @patch("rlsbl.commands.release.validate_clean_tree", return_value=set())
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets", return_value="pypi")
    @patch("rlsbl.commands.release._abort_on_scaffold_conflicts")
    @patch("rlsbl.commands.release.resolve_target_paths", return_value={})
    @patch("rlsbl.commands.release.compute_release_version", return_value=("0.1.0", "0.1.1", "patch", "v0.1.1"))
    @patch("rlsbl.commands.release.extract_changelog_entry_from_text", return_value="- test")
    @patch("rlsbl.commands.release.run", return_value="")
    @patch("rlsbl.commands.release.parse_porcelain_paths", return_value=set())
    @patch("rlsbl.commands.release.build_hook_env", return_value={})
    @patch("rlsbl.commands.release.get_hook_timeout", return_value=30)
    @patch("rlsbl.commands.release.is_hook_customized", return_value=False)
    @patch("rlsbl.commands.release._run_builtin_tests")
    @patch("rlsbl.commands.release._run_builtin_lint")
    @patch("rlsbl.commands.release._run_strictcli_schema_dump")
    @patch("rlsbl.commands.release._run_selfdoc_gen")
    @patch("rlsbl.commands.release._run_selfdoc_check")
    @patch("rlsbl.commands.release._run_selfdoc_post_generate")
    def test_skip_lock_true_skips_acquire(
        self, _selfdoc_post, _selfdoc_check, _selfdoc_gen, _schema_dump,
        _lint, _tests, _hook_empty, _hook_timeout, _hook_env,
        _porcelain, _run, _extract, _compute, _resolve_targets,
        _scaffold, _validate_targets_top, _validate_ota, _validate_config,
        _validate_gh, _validate_pipeline, _validate_clean, _validate_branch,
        _resolve_mono, _validate_blog, _validate_changelog, _gen_changelog,
        _dry_run_summary, _resolve_release_targets, _mutating,
        mock_acquire, mock_release,
        tmp_path,
    ):
        """With skip-lock=True, acquire_lock and release_lock are NOT called."""
        from rlsbl.commands.release import _run_cmd_inner

        ctx = _make_release_context(tmp_path)
        config = _make_release_config()
        flags = {"skip-lock": True, "dry-run": True}

        _run_cmd_inner(config, flags, ctx=ctx)

        mock_acquire.assert_not_called()
        mock_release.assert_not_called()

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release._run_release_mutating")
    @patch("rlsbl.commands.release.resolve_release_targets", return_value=[])
    @patch("rlsbl.commands.release.print_dry_run_summary")
    @patch("rlsbl.commands.release.generate_changelog", return_value="# Changelog\n\n## 0.1.1\n\n- test\n")
    @patch("rlsbl.commands.release.validate_changelog_state", return_value=None)
    @patch("rlsbl.commands.release.validate_blog_body", return_value=(None, None))
    @patch("rlsbl.commands.release.resolve_monorepo_context", return_value=(None, None, False, False, None))
    @patch("rlsbl.commands.release.validate_branch_and_remote", return_value="main")
    @patch("rlsbl.commands.release.validate_clean_tree", return_value=set())
    @patch("rlsbl.commands.release.validate_pipeline_config")
    @patch("rlsbl.commands.release.validate_gh_cli")
    @patch("rlsbl.commands.release.validate_config_integrity")
    @patch("rlsbl.commands.release.validate_ota_mode")
    @patch("rlsbl.commands.release.validate_release_targets", return_value="pypi")
    @patch("rlsbl.commands.release._abort_on_scaffold_conflicts")
    @patch("rlsbl.commands.release.resolve_target_paths", return_value={})
    @patch("rlsbl.commands.release.compute_release_version", return_value=("0.1.0", "0.1.1", "patch", "v0.1.1"))
    @patch("rlsbl.commands.release.extract_changelog_entry_from_text", return_value="- test")
    @patch("rlsbl.commands.release.run", return_value="")
    @patch("rlsbl.commands.release.parse_porcelain_paths", return_value=set())
    @patch("rlsbl.commands.release.build_hook_env", return_value={})
    @patch("rlsbl.commands.release.get_hook_timeout", return_value=30)
    @patch("rlsbl.commands.release.is_hook_customized", return_value=False)
    @patch("rlsbl.commands.release._run_builtin_tests")
    @patch("rlsbl.commands.release._run_builtin_lint")
    @patch("rlsbl.commands.release._run_strictcli_schema_dump")
    @patch("rlsbl.commands.release._run_selfdoc_gen")
    @patch("rlsbl.commands.release._run_selfdoc_check")
    @patch("rlsbl.commands.release._run_selfdoc_post_generate")
    def test_without_skip_lock_calls_acquire(
        self, _selfdoc_post, _selfdoc_check, _selfdoc_gen, _schema_dump,
        _lint, _tests, _hook_empty, _hook_timeout, _hook_env,
        _porcelain, _run, _extract, _compute, _resolve_targets,
        _scaffold, _validate_targets_top, _validate_ota, _validate_config,
        _validate_gh, _validate_pipeline, _validate_clean, _validate_branch,
        _resolve_mono, _validate_blog, _validate_changelog, _gen_changelog,
        _dry_run_summary, _resolve_release_targets, _mutating,
        mock_acquire, mock_release,
        tmp_path,
    ):
        """Without skip-lock, acquire_lock IS called (dry-run exits before lock)."""
        from rlsbl.commands.release import _run_cmd_inner

        ctx = _make_release_context(tmp_path)
        config = _make_release_config()
        # No skip-lock, but use non-dry-run to hit the lock path
        flags = {}

        _run_cmd_inner(config, flags, ctx=ctx)

        mock_acquire.assert_called_once()
        mock_release.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Batch release passes skip-lock=True to inner releases
# ---------------------------------------------------------------------------


class TestBatchReleasePassesSkipLock:
    """Test that batch release functions pass skip-lock=True to run_cmd."""

    def _setup_workspace(self, tmp_path, projects_data):
        """Create a minimal workspace with project files."""
        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")

        (tmp_path / "README.md").write_text("# test\n")
        run_git(tmp_path, "add", "README.md")
        run_git(tmp_path, "commit", "-q", "-m", "initial")

        make_workspace(tmp_path, projects_data)

        for proj in projects_data:
            proj_dir = tmp_path / proj["path"]
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "pyproject.toml").write_text(
                f'[project]\nname = "{proj["name"]}"\nversion = "0.1.0"\n'
            )

        run_git(tmp_path, "add", WORKSPACE_DIR)
        for proj in projects_data:
            run_git(tmp_path, "add", proj["path"])
        run_git(tmp_path, "commit", "-q", "-m", "add workspace")

    @patch("rlsbl.commands.monorepo.batch_release.rlsbl_lock")
    def test_batch_packages_passes_skip_lock(self, mock_lock, tmp_path, monkeypatch):
        """_batch_release_packages passes skip-lock=True in release_flags."""
        monkeypatch.chdir(tmp_path)

        projects_data = [
            {"path": "pkg-a", "name": "pkg-a"},
            {"path": "pkg-b", "name": "pkg-b"},
        ]
        self._setup_workspace(tmp_path, projects_data)

        from rlsbl.workspace import load_workspace
        projects = load_workspace(str(tmp_path))

        batch_config = BatchReleaseConfig(
            section_type="packages",
            packages={
                "pkg-a": ReleaseConfig(bump="patch", include=[], exclude=[], description="release a"),
            },
        )

        batch_path = str(tmp_path / ".rlsbl-monorepo" / "releases" / "unreleased.toml")
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        Path(batch_path).write_text("")

        # Mock the context manager to yield immediately
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        captured_flags = {}

        def fake_run_cmd(release_config, release_flags, *, ctx):
            captured_flags.update(release_flags)

        with patch("rlsbl.commands.monorepo.batch_release.WorkspaceGraph") as mock_graph:
            mock_graph.return_value.topological_order.return_value = ["pkg-a", "pkg-b"]
            with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
                from rlsbl.commands.monorepo import batch_release
                with patch.object(batch_release, "__import__", create=True):
                    with patch("rlsbl.commands.release.run_cmd", side_effect=fake_run_cmd) as mock_run:
                        from rlsbl.commands.monorepo.batch_release import _batch_release_packages
                        _batch_release_packages(
                            {"dry-run": False, "yes": True, "quiet": True},
                            str(tmp_path), batch_path, batch_config, projects,
                        )

        assert captured_flags.get("skip-lock") is True
        mock_lock.assert_called_once_with(".rlsbl-monorepo", project_root=str(tmp_path))

    @patch("rlsbl.commands.monorepo.batch_release.rlsbl_lock")
    def test_batch_releasables_passes_skip_lock(self, mock_lock, tmp_path, monkeypatch):
        """_batch_release_releasables passes skip-lock=True in release_flags."""
        monkeypatch.chdir(tmp_path)

        projects_data = [
            {"path": "pkg-a", "name": "pkg-a"},
        ]
        self._setup_workspace(tmp_path, projects_data)

        from rlsbl.workspace import load_workspace
        projects = load_workspace(str(tmp_path))

        batch_config = BatchReleaseConfig(
            section_type="releasables",
            packages={
                "my-rel": ReleaseConfig(bump="patch", include=[], exclude=[], description="release rel"),
            },
        )

        batch_path = str(tmp_path / ".rlsbl-monorepo" / "releases" / "unreleased.toml")
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        Path(batch_path).write_text("")

        # Mock the context manager
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        captured_flags = {}

        def fake_run_cmd(release_config, release_flags, *, ctx):
            captured_flags.update(release_flags)

        # Mock releasable resolution
        from types import SimpleNamespace
        fake_releasable = SimpleNamespace(name="my-rel", tag_format=None)

        with patch("rlsbl.commands.monorepo.batch_release.WorkspaceGraph") as mock_graph:
            mock_graph.return_value.topological_order.return_value = ["pkg-a"]
            with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
                with patch("rlsbl.commands.release.run_cmd", side_effect=fake_run_cmd):
                    with patch("rlsbl.workspace.load_releasables", return_value=[fake_releasable]):
                        with patch("rlsbl.workspace.members_of", return_value=[projects[0]]):
                            from rlsbl.commands.monorepo.batch_release import _batch_release_releasables
                            _batch_release_releasables(
                                {"dry-run": False, "yes": True, "quiet": True},
                                str(tmp_path), batch_path, batch_config, projects,
                            )

        assert captured_flags.get("skip-lock") is True
        mock_lock.assert_called_once_with(".rlsbl-monorepo", project_root=str(tmp_path))
