"""Regression test for plain-target dev_node scaffold root resolution.

Bug: when running `rlsbl scaffold --target plain` in a monorepo sub-project,
`detect_registries()` returns empty (PlainTarget.detect() always returns
False), so cmd_scaffold falls through to `find_project_root()` which walks
up and finds the monorepo root. The scaffold_root is then the monorepo root
instead of cwd, causing `_is_dev_node_project()` to fail (it can't resolve
the project from the wrong root). This means changelog infrastructure
(unreleased.jsonl, CHANGELOG.md) gets created for dev_node projects that
should not have it.

Fix: when --target is explicitly passed, always use Path.cwd() as
scaffold_root.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import run_cmd, _is_dev_node_project
from rlsbl.context import ProjectContext, create_context
from tests.conftest import make_workspace


class TestScaffoldPlainDevNode:
    """Scaffolding a plain-target dev_node project must skip changelog files."""

    def _setup_monorepo_with_dev_node(self, mock_git_repo, subdir="infra"):
        """Create a monorepo with a plain dev_node sub-project.

        Returns the sub-project directory path.
        """
        proj_dir = mock_git_repo / subdir
        proj_dir.mkdir()

        # Set up workspace.toml with the project marked as dev_node
        make_workspace(mock_git_repo, [
            {"path": subdir, "name": subdir, "dev_node": True},
        ])

        return proj_dir

    def test_is_dev_node_with_correct_root(self, mock_git_repo, monkeypatch):
        """_is_dev_node_project returns True when project_root points to the
        sub-project directory (the fixed behavior)."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # With project_root = sub-project dir (correct), dev_node is detected
        assert _is_dev_node_project(proj_dir) is True

    def test_is_dev_node_with_wrong_root(self, mock_git_repo, monkeypatch):
        """_is_dev_node_project returns False when project_root points to the
        monorepo root (the buggy behavior)."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # With project_root = monorepo root (wrong), dev_node is NOT detected
        # because resolve_project(ws_root, monorepo_root) can't match the
        # sub-project
        assert _is_dev_node_project(mock_git_repo) is False

    def test_scaffold_plain_dev_node_no_changelog(self, mock_git_repo, monkeypatch):
        """Scaffolding a plain dev_node project must NOT create changelog files."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # Create context with project_root = sub-project dir (the fix)
        ctx = create_context(proj_dir)

        # Run scaffold for the plain target
        run_cmd("plain", [], {
            "no-commit": True,
            "no-tag": True,
            "skip-shared": False,
        }, ctx=ctx)

        # Dev node projects must NOT have changelog infrastructure
        changelog = proj_dir / "CHANGELOG.md"
        unreleased = proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl"

        assert not changelog.exists(), (
            "CHANGELOG.md should not be created for dev_node projects"
        )
        assert not unreleased.exists(), (
            "unreleased.jsonl should not be created for dev_node projects"
        )

    def test_scaffold_plain_non_dev_node_has_changelog(self, mock_git_repo, monkeypatch):
        """Scaffolding a plain NON-dev_node project creates changelog files normally."""
        proj_dir = mock_git_repo / "lib"
        proj_dir.mkdir()

        # Set up workspace without dev_node flag
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
        ])

        monkeypatch.chdir(proj_dir)
        ctx = create_context(proj_dir)

        run_cmd("plain", [], {
            "no-commit": True,
            "no-tag": True,
            "skip-shared": False,
        }, ctx=ctx)

        # Non-dev-node projects SHOULD have changelog infrastructure
        changelog = proj_dir / "CHANGELOG.md"
        unreleased = proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl"

        assert changelog.exists(), (
            "CHANGELOG.md should be created for non-dev_node projects"
        )
        assert unreleased.exists(), (
            "unreleased.jsonl should be created for non-dev_node projects"
        )
