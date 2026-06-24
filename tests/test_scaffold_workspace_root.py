"""Tests for skipping CI template generation at monorepo workspace roots.

When `rlsbl scaffold` runs at a workspace root, CI templates (ci.yml,
publish.yml) should be skipped because workspace roots are not importable
Python packages -- the ci-router handles per-package CI. Non-CI scaffold
files (gitignore, hooks, changelog) should still be generated.
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import (
    _is_workspace_root,
    run_cmd,
    run_cmd_multi,
)
from rlsbl.context import ProjectContext

from conftest import make_workspace


def _ctx(root="."):
    """Create a minimal ProjectContext for scaffold tests."""
    return ProjectContext(
        project_root=Path(root), workspace_root=None, config={},
    )


class TestIsWorkspaceRoot:
    """Unit tests for _is_workspace_root detection."""

    def test_returns_true_when_workspace_toml_exists(self, tmp_path):
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("[[projects]]\n")
        assert _is_workspace_root(tmp_path) is True

    def test_returns_false_when_no_workspace_dir(self, tmp_path):
        assert _is_workspace_root(tmp_path) is False

    def test_returns_false_when_dir_exists_but_no_toml(self, tmp_path):
        (tmp_path / ".rlsbl-monorepo").mkdir()
        assert _is_workspace_root(tmp_path) is False

    def test_returns_false_for_none(self):
        assert _is_workspace_root(None) is False

    def test_returns_false_for_sub_project(self, tmp_path):
        """A sub-project directory that is NOT the workspace root."""
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text("[[projects]]\n")
        sub = tmp_path / "packages" / "core"
        sub.mkdir(parents=True)
        # The sub-project itself does not have .rlsbl-monorepo/workspace.toml
        assert _is_workspace_root(sub) is False


class TestRunCmdWorkspaceRoot:
    """run_cmd at a workspace root skips CI and publish templates."""

    def test_workspace_root_skips_ci_templates(self, mock_git_repo):
        """Scaffold at a workspace root must NOT create ci.yml or publish.yml."""
        root = mock_git_repo

        # Set up as a pypi project at the workspace root
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "ws-root-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        # Make it a workspace root
        make_workspace(root, [
            {"path": "packages/core", "name": "core"},
        ])

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("pypi", [], {"no-commit": True, "no-tag": True}, ctx=_ctx())

        ci_yml = root / ".github" / "workflows" / "ci.yml"
        publish_yml = root / ".github" / "workflows" / "publish.yml"

        assert not ci_yml.exists(), (
            "ci.yml should not be created at a workspace root"
        )
        assert not publish_yml.exists(), (
            "publish.yml should not be created at a workspace root"
        )

    def test_workspace_root_skips_all_scaffold(self, mock_git_repo):
        """Scaffold at a workspace root skips entirely -- creates nothing."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "ws-root-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        make_workspace(root, [
            {"path": "packages/core", "name": "core"},
        ])

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("pypi", [], {"no-commit": True, "no-tag": True}, ctx=_ctx())

        # Workspace roots are not packages -- scaffold returns early
        assert not (root / ".rlsbl").exists(), (
            ".rlsbl/ should not be created at a workspace root"
        )
        assert not (root / ".gitignore").exists(), (
            ".gitignore should not be created at a workspace root"
        )

    def test_non_workspace_root_creates_ci_templates(self, mock_git_repo):
        """Scaffold at a non-workspace-root project creates CI templates normally."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "normal-project"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        # No .rlsbl-monorepo -- this is a standalone project

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd("pypi", [], {"no-commit": True, "no-tag": True}, ctx=_ctx())

        ci_yml = root / ".github" / "workflows" / "ci.yml"
        assert ci_yml.exists(), (
            "ci.yml should be created for non-workspace-root projects"
        )


class TestRunCmdMultiWorkspaceRoot:
    """run_cmd_multi at a workspace root skips CI and publish templates."""

    def test_workspace_root_skips_all_ci_and_publish(self, mock_git_repo):
        """Multi-target scaffold at workspace root must NOT create CI or publish."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "ws-multi-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "go.mod").write_text(
            "module github.com/test/ws-multi-test\n\ngo 1.23\n"
        )
        make_workspace(root, [
            {"path": "packages/core", "name": "core"},
        ])

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "go"], [], {"no-commit": True, "no-tag": True}, ctx=_ctx())

        ci_pypi = root / ".github" / "workflows" / "ci-pypi.yml"
        ci_go = root / ".github" / "workflows" / "ci-go.yml"
        publish = root / ".github" / "workflows" / "publish.yml"

        assert not ci_pypi.exists(), (
            "ci-pypi.yml should not be created at a workspace root"
        )
        assert not ci_go.exists(), (
            "ci-go.yml should not be created at a workspace root"
        )
        assert not publish.exists(), (
            "publish.yml should not be created at a workspace root"
        )

    def test_workspace_root_multi_skips_all_scaffold(self, mock_git_repo):
        """Multi-target scaffold at workspace root skips entirely -- creates nothing."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "ws-multi-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "go.mod").write_text(
            "module github.com/test/ws-multi-test\n\ngo 1.23\n"
        )
        make_workspace(root, [
            {"path": "packages/core", "name": "core"},
        ])

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "go"], [], {"no-commit": True, "no-tag": True}, ctx=_ctx())

        # Workspace roots are not packages -- scaffold returns early
        assert not (root / ".rlsbl").exists(), (
            ".rlsbl/ should not be created at a workspace root (multi)"
        )
        assert not (root / ".gitignore").exists(), (
            ".gitignore should not be created at a workspace root (multi)"
        )
