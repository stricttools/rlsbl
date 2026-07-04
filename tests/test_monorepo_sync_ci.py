"""Tests for per-target CI file support in monorepo sync."""

import json
import os
import stat
import subprocess
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import (
    _cmd_init,
    _cmd_add,
    _cmd_sync,
    _generate_router,
)
from rlsbl.workspace import load_workspace, save_workspace


CI_WORKFLOW = """\
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo test
"""

CI_PYPI_WORKFLOW = """\
name: CI PyPI

on:
  push:
    branches: [main]

jobs:
  test-pypi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: uv run pytest
"""

CI_GO_WORKFLOW = """\
name: CI Go

on:
  push:
    branches: [main]

jobs:
  test-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: go test ./...
"""


def _make_project_with_ci_files(base_path, subdir, ci_files, name=None):
    """Create a minimal npm project with specific CI workflow files.

    ci_files: dict of filename -> content, e.g. {"ci.yml": ..., "ci-pypi.yml": ...}
    """
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg_name = name or os.path.basename(subdir)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": pkg_name, "version": "0.1.0"}, f)

    wf_dir = os.path.join(proj_dir, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    for filename, content in ci_files.items():
        with open(os.path.join(wf_dir, filename), "w") as f:
            f.write(content)

    return subdir


class TestPerTargetCIFiles:
    """Tests for syncing per-target CI files (ci-pypi.yml, ci-go.yml, etc.)."""

    def test_sync_per_target_ci_files(self, mock_git_repo, capsys):
        """Sub-project with ci-pypi.yml and ci-go.yml (no ci.yml) syncs both."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW, "ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        # Remove the scaffold-generated ci.yml so only per-target files remain
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        # Both per-target files should exist in shared workflows dir
        wf_dir = mock_git_repo / ".github" / "workflows"
        assert (wf_dir / "tooling-ci-pypi.yml").exists()
        assert (wf_dir / "tooling-ci-go.yml").exists()
        # Old single-file name should NOT exist
        assert not (wf_dir / "tooling-ci.yml").exists()

    def test_per_target_trigger_rewrite(self, mock_git_repo, capsys):
        """Per-target CI files get their triggers rewritten to workflow_call."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        # Remove scaffold-generated ci.yml
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        dest = mock_git_repo / ".github" / "workflows" / "tooling-ci-pypi.yml"
        content = dest.read_text()
        assert "workflow_call" in content
        assert "push:" not in content

    def test_per_target_header_comment(self, mock_git_repo, capsys):
        """Per-target CI files have header referencing the actual source file name."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        dest = mock_git_repo / ".github" / "workflows" / "tooling-ci-pypi.yml"
        content = dest.read_text()
        assert "# Source: tooling/.github/workflows/ci-pypi.yml" in content

    def test_per_target_read_only(self, mock_git_repo, capsys):
        """Per-target CI files are written as read-only."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        dest = mock_git_repo / ".github" / "workflows" / "tooling-ci-go.yml"
        mode = stat.S_IMODE(os.stat(str(dest)).st_mode)
        assert mode == 0o444

    def test_per_target_working_directory_injected(self, mock_git_repo, capsys):
        """Per-target CI files get working-directory injected."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        dest = mock_git_repo / ".github" / "workflows" / "tooling-ci-pypi.yml"
        content = dest.read_text()
        assert "working-directory: tooling" in content


class TestSingleCIBackwardCompat:
    """Verify that the old single ci.yml pattern still works identically."""

    def test_single_ci_yml_synced_as_before(self, mock_git_repo, capsys):
        """Sub-project with only ci.yml still produces {name}-ci.yml."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "core",
            {"ci.yml": CI_WORKFLOW},
        )
        _cmd_add(["core"], {}, project_root=".")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        wf_dir = mock_git_repo / ".github" / "workflows"
        assert (wf_dir / "core-ci.yml").exists()
        content = (wf_dir / "core-ci.yml").read_text()
        assert "# Source: core/.github/workflows/ci.yml" in content
        assert "workflow_call" in content

    def test_mixed_single_and_per_target(self, mock_git_repo, capsys):
        """Project with ci.yml AND ci-pypi.yml produces all expected files."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "hybrid",
            {"ci.yml": CI_WORKFLOW, "ci-pypi.yml": CI_PYPI_WORKFLOW},
        )
        _cmd_add(["hybrid"], {}, project_root=".")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        wf_dir = mock_git_repo / ".github" / "workflows"
        assert (wf_dir / "hybrid-ci.yml").exists()
        assert (wf_dir / "hybrid-ci-pypi.yml").exists()


class TestRouterMultipleCIFiles:
    """Test that the CI router handles multiple CI files per project."""

    def test_router_single_ci_backward_compat(self):
        """Project without _ci_files still generates a single job."""
        projects = [
            {"name": "core", "path": "core"},
        ]
        content = _generate_router(projects)
        assert "uses: ./.github/workflows/core-ci.yml" in content

    def test_router_single_ci_with_ci_files(self):
        """Project with one _ci_files entry uses CI filename as job key."""
        projects = [
            {"name": "core", "path": "core", "_ci_files": ["core-ci.yml"]},
        ]
        content = _generate_router(projects)
        assert "uses: ./.github/workflows/core-ci.yml" in content

    def test_router_multiple_ci_files(self):
        """Project with multiple _ci_files generates one job per CI file."""
        projects = [
            {
                "name": "tooling",
                "path": "tooling",
                "_ci_files": ["tooling-ci-pypi.yml", "tooling-ci-go.yml"],
            },
        ]
        content = _generate_router(projects)
        assert "uses: ./.github/workflows/tooling-ci-pypi.yml" in content
        assert "uses: ./.github/workflows/tooling-ci-go.yml" in content
        # Both jobs should check the same project output
        assert content.count("needs.detect.outputs.tooling == 'true'") == 2

    def test_router_mixed_single_and_multi(self):
        """Mix of single-CI and multi-CI projects in same router."""
        projects = [
            {"name": "core", "path": "core", "_ci_files": ["core-ci.yml"]},
            {
                "name": "tooling",
                "path": "tooling",
                "_ci_files": ["tooling-ci-pypi.yml", "tooling-ci-go.yml"],
            },
        ]
        content = _generate_router(projects)
        # core: single job
        assert "uses: ./.github/workflows/core-ci.yml" in content
        # tooling: two jobs
        assert "uses: ./.github/workflows/tooling-ci-pypi.yml" in content
        assert "uses: ./.github/workflows/tooling-ci-go.yml" in content


class TestCleanupPerTargetCI:
    """Test that stale per-target CI files are cleaned up when projects are removed."""

    def test_cleanup_removes_per_target_ci(self, mock_git_repo, capsys):
        """Removing a project cleans up its per-target CI files."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW, "ci-go.yml": CI_GO_WORKFLOW},
        )
        _make_project_with_ci_files(
            mock_git_repo, "core",
            {"ci.yml": CI_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        _cmd_add(["core"], {}, project_root=".")
        # Remove scaffold-generated ci.yml from tooling (only per-target files)
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
        capsys.readouterr()

        # Verify all CI files exist
        wf_dir = mock_git_repo / ".github" / "workflows"
        assert (wf_dir / "tooling-ci-pypi.yml").exists()
        assert (wf_dir / "tooling-ci-go.yml").exists()
        assert (wf_dir / "core-ci.yml").exists()

        # Remove "tooling" from workspace
        from rlsbl.commands.monorepo import _cmd_remove
        _cmd_remove(["tooling"], {}, project_root=".")
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "remove tooling"],
            cwd=str(mock_git_repo), check=True,
        )

        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")

        # tooling CI files should be removed
        assert not (wf_dir / "tooling-ci-pypi.yml").exists()
        assert not (wf_dir / "tooling-ci-go.yml").exists()
        # core CI file should still exist
        assert (wf_dir / "core-ci.yml").exists()

    def test_cleanup_does_not_remove_current_project_ci(self, mock_git_repo, capsys):
        """Cleanup does not touch CI files belonging to current projects."""
        _cmd_init({}, project_root=".")
        _make_project_with_ci_files(
            mock_git_repo, "tooling",
            {"ci-pypi.yml": CI_PYPI_WORKFLOW, "ci-go.yml": CI_GO_WORKFLOW},
        )
        _cmd_add(["tooling"], {}, project_root=".")
        scaffold_ci = mock_git_repo / "tooling" / ".github" / "workflows" / "ci.yml"
        if scaffold_ci.exists():
            scaffold_ci.unlink()
        subprocess.run(["git", "add", "."], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"],
            cwd=str(mock_git_repo), check=True,
        )

        # Sync twice to ensure cleanup doesn't remove active files
        with patch("rlsbl.utils.find_commit_tool", return_value="git"):
            _cmd_sync({}, project_root=".")
            capsys.readouterr()
            _cmd_sync({}, project_root=".")

        wf_dir = mock_git_repo / ".github" / "workflows"
        assert (wf_dir / "tooling-ci-pypi.yml").exists()
        assert (wf_dir / "tooling-ci-go.yml").exists()
