"""Tests for enhanced dry-run summary in batch release."""

import json
import os
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo.batch_release import _cmd_batch_release
from rlsbl.release_file import get_batch_release_file_path
from rlsbl.workspace import save_workspace, WORKSPACE_DIR


def _write_toml(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_npm_project(base_path, subdir, version="0.1.0", deps=None):
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    if deps:
        pkg["dependencies"] = deps
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)


def _init_workspace(base_path, projects):
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)
    save_workspace(str(base_path), projects)


BATCH_TOML_3PKG = (
    '[packages.auth]\n'
    'bump = "patch"\ndescription = "Fix auth token refresh"\n'
    'include = ["npm"]\nexclude = []\n'
    '\n'
    '[packages.sdk]\n'
    'bump = "minor"\ndescription = "Add streaming API"\n'
    'include = ["npm"]\nexclude = []\n'
    '\n'
    '[packages.platform]\n'
    'bump = "minor"\ndescription = "New deployment targets"\n'
    'include = ["npm"]\nexclude = []\n'
)


class TestBatchDryRunSummary:
    """Dry-run output includes bump type and description for each item."""

    def test_dryrun_packages_shows_detail(self, mock_git_repo, capsys, bypass_upfront_validation):
        """In dry-run mode, per-package bump and description are printed."""
        # sdk depends on auth, platform depends on sdk
        # -> topo order: auth, sdk, platform
        _make_npm_project(mock_git_repo, "auth")
        _make_npm_project(mock_git_repo, "sdk", deps={"auth": "^0.1.0"})
        _make_npm_project(mock_git_repo, "platform", deps={"sdk": "^0.1.0"})

        projects = [
            {"path": "auth", "name": "auth"},
            {"path": "sdk", "name": "sdk"},
            {"path": "platform", "name": "platform"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, BATCH_TOML_3PKG)

        def mock_run_cmd(release_config, flags, **kwargs):
            pass  # no-op in dry-run

        with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            with patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
                _cmd_batch_release(
                    {"dry-run": True, "yes": True, "quiet": False},
                    project_root=mock_git_repo,
                )

        captured = capsys.readouterr()
        out = captured.out

        # Verify per-item detail lines are present
        assert "1. auth (patch)" in out
        assert "Fix auth token refresh" in out
        assert "2. sdk (minor)" in out
        assert "Add streaming API" in out
        assert "3. platform (minor)" in out
        assert "New deployment targets" in out

    def test_non_dryrun_no_detail(self, mock_git_repo, capsys, bypass_upfront_validation):
        """In non-dry-run mode, per-package detail lines are NOT printed."""
        _make_npm_project(mock_git_repo, "auth")
        _make_npm_project(mock_git_repo, "sdk", deps={"auth": "^0.1.0"})
        _make_npm_project(mock_git_repo, "platform", deps={"sdk": "^0.1.0"})

        projects = [
            {"path": "auth", "name": "auth"},
            {"path": "sdk", "name": "sdk"},
            {"path": "platform", "name": "platform"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, BATCH_TOML_3PKG)

        def mock_run_cmd(release_config, flags, **kwargs):
            pass

        with patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"):
            with patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
                _cmd_batch_release(
                    {"dry-run": False, "yes": True, "quiet": False},
                    project_root=mock_git_repo,
                )

        captured = capsys.readouterr()
        out = captured.out

        # The summary header is printed, but NOT the numbered detail lines
        assert "Batch release: 3 package(s)" in out
        assert "1. auth (patch)" not in out
        assert "2. sdk (minor)" not in out
        assert "3. platform (minor)" not in out

    def test_dryrun_releasables_shows_detail(self, mock_git_repo, capsys, bypass_upfront_validation):
        """In dry-run mode with releasables, per-releasable detail is printed."""
        _make_npm_project(mock_git_repo, "core-lib")
        _make_npm_project(mock_git_repo, "web-app", deps={"core-lib": "^0.1.0"})

        projects = [
            {"path": "core-lib", "name": "core-lib"},
            {"path": "web-app", "name": "web-app"},
        ]
        _init_workspace(mock_git_repo, projects)

        batch_toml = (
            '[releasables.core]\n'
            'bump = "patch"\ndescription = "Core stability fixes"\n'
            'include = ["npm"]\nexclude = []\n'
            '\n'
            '[releasables.web]\n'
            'bump = "minor"\ndescription = "New dashboard UI"\n'
            'include = ["npm"]\nexclude = []\n'
        )
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        _write_toml(batch_path, batch_toml)

        # Mock load_releasables and members_of for explicit mode
        from types import SimpleNamespace

        fake_releasables = [
            SimpleNamespace(name="core", members=["core-lib"]),
            SimpleNamespace(name="web", members=["web-app"]),
        ]

        def fake_members_of(rel_name, projs):
            mapping = {
                "core": [{"name": "core-lib", "path": "core-lib"}],
                "web": [{"name": "web-app", "path": "web-app"}],
            }
            return mapping.get(rel_name, [])

        def mock_run_cmd(release_config, flags, **kwargs):
            pass

        with patch("rlsbl.commands.monorepo.batch_release.is_explicit_mode", return_value=True), \
             patch("rlsbl.workspace.load_releasables",
                   return_value=fake_releasables), \
             patch("rlsbl.workspace.members_of",
                   side_effect=fake_members_of), \
             patch("rlsbl.commands.monorepo.batch_release._finalize_batch_file"), \
             patch("rlsbl.commands.release.run_cmd", mock_run_cmd):
            _cmd_batch_release(
                {"dry-run": True, "yes": True, "quiet": False},
                project_root=mock_git_repo,
            )

        captured = capsys.readouterr()
        out = captured.out

        assert "Batch release: 2 releasable(s)" in out
        # Check detail lines (order depends on topo sort)
        assert "(patch)" in out
        assert "Core stability fixes" in out
        assert "(minor)" in out
        assert "New dashboard UI" in out
