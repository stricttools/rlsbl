"""Tests for detect_uv_workspace_root."""

import os
import textwrap

import pytest

from rlsbl.utils import detect_uv_workspace_root


class TestDetectUvWorkspaceRoot:
    """Tests for detect_uv_workspace_root(project_dir)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.root = str(tmp_path)

    def _write_pyproject(self, directory, content):
        """Write a pyproject.toml with the given content."""
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "pyproject.toml")
        with open(path, "w") as f:
            f.write(textwrap.dedent(content))

    def test_literal_member(self):
        """Project IS a uv workspace member (literal name in members list)."""
        workspace = self.root
        project = os.path.join(workspace, "packages", "core")
        os.makedirs(project)
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["packages/core"]
        """)
        result = detect_uv_workspace_root(project)
        assert result == workspace

    def test_glob_member(self):
        """Project IS a uv workspace member (matched by * glob)."""
        workspace = self.root
        project = os.path.join(workspace, "packages", "web")
        os.makedirs(project)
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["packages/*"]
        """)
        result = detect_uv_workspace_root(project)
        assert result == workspace

    def test_not_a_member(self):
        """Project is NOT a member (not in members list, not matched by globs)."""
        workspace = self.root
        project = os.path.join(workspace, "other", "unrelated")
        os.makedirs(project)
        os.makedirs(os.path.join(workspace, "packages", "core"))
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["packages/*"]
        """)
        result = detect_uv_workspace_root(project)
        assert result is None

    def test_excluded_member(self):
        """Project is excluded (in both members glob and exclude list)."""
        workspace = self.root
        project = os.path.join(workspace, "packages", "internal")
        os.makedirs(project)
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["packages/*"]
            exclude = ["packages/internal"]
        """)
        result = detect_uv_workspace_root(project)
        assert result is None

    def test_no_workspace_root(self):
        """No workspace root found (no parent has [tool.uv.workspace])."""
        project = os.path.join(self.root, "some", "project")
        os.makedirs(project)
        # Write a pyproject.toml without workspace config
        self._write_pyproject(self.root, """\
            [project]
            name = "top-level"
        """)
        result = detect_uv_workspace_root(project)
        assert result is None

    def test_workspace_root_itself(self):
        """project_dir IS the workspace root itself -- returns None (root is not a 'member')."""
        workspace = self.root
        os.makedirs(os.path.join(workspace, "packages", "core"))
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["packages/*"]
        """)
        result = detect_uv_workspace_root(workspace)
        assert result is None

    def test_nested_two_levels_up(self):
        """Workspace root is 2 levels up from project_dir."""
        workspace = self.root
        project = os.path.join(workspace, "libs", "python", "mylib")
        os.makedirs(project)
        self._write_pyproject(workspace, """\
            [tool.uv.workspace]
            members = ["libs/python/*"]
        """)
        result = detect_uv_workspace_root(project)
        assert result == workspace
