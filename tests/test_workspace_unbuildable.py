"""Tests for the workspace-unbuildable check.

Two layouts, two strategies (chosen explicitly from the repo layout, never
silently): a root uv workspace gets one root-level ``uv sync --all-packages``;
a polyglot monorepo with no root uv workspace gets a per-pypi-project
``uv sync`` in each project's own directory.
"""


def _make_root_uv_workspace(root, members):
    """Declare the repo root as a uv workspace root over *members*."""
    member_list = ", ".join(f'"{m}"' for m in members)
    (root / "pyproject.toml").write_text(
        f"[tool.uv.workspace]\nmembers = [{member_list}]\n"
    )

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.context import ProjectContext


class TestWorkspaceUnbuildableSkips:
    """The check skips when the context is not a workspace or has no pypi targets."""

    def test_skips_non_workspace(self, mock_git_repo):
        """Non-workspace context -> skip (via scope adapter)."""
        from strictcli import SkipCheck
        from rlsbl.checks.scope import scope_adapter

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert isinstance(result, SkipCheck)
        assert "not a monorepo" in result.reason

    def test_skips_no_pypi_targets(self, mock_git_repo):
        """Workspace with no pypi-target projects -> skip."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text('{"name":"mylib","version":"1.0.0"}')

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )
        result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "skip"
        assert "no pypi-target" in result.message

    def test_fails_uv_not_installed(self, mock_git_repo):
        """uv not installed -> hard fail (a pypi workspace cannot be verified)."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        _make_root_uv_workspace(mock_git_repo, ["mylib"])

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )
        with patch("subprocess.run", side_effect=FileNotFoundError("uv")):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "fail"
        assert "uv is not installed" in result.message


class TestRootWorkspaceRecognition:
    """Which strategy the check picks comes from ONE uv-workspace locator.

    A root that declares ``[tool.uv.workspace]`` AND ``[project]`` is not a
    virtual root, so the layout question is answered entirely by resolving a
    member up to it -- which is the locator's job, and the reason there is now
    only one of them.
    """

    def _package_root(self, root, members):
        """A root that is both a distributable package and a workspace root."""
        member_list = ", ".join(f'"{m}"' for m in members)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "rootpkg"\nversion = "0.1.0"\n\n'
            f"[tool.uv.workspace]\nmembers = [{member_list}]\n"
        )

    def _member(self, root, relpath, name):
        directory = root / relpath
        directory.mkdir(parents=True)
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
        )
        return directory

    def _ctx(self, project_root, workspace_root, relpath, name):
        return WorkspaceCheckContext(
            project_root=project_root,
            workspace_root=workspace_root,
            config={},
            projects=[{"path": relpath, "name": name}],
            graph=None,
        )

    def _calls(self, ctx):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs.get("cwd")))
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        return result, calls

    def test_a_recursive_members_glob_claims_its_member(self, mock_git_repo):
        """``**`` crosses directory separators, which is how uv reads it.

        Expanding it without ``recursive=True`` made it behave like ``*``, so a
        member one level deeper than the glob's first segment did not resolve
        up to the root and the whole workspace was synced project by project.
        """
        self._package_root(mock_git_repo, ["packages/**"])
        self._member(mock_git_repo, "packages/group/pylib", "pylib")

        result, calls = self._calls(
            self._ctx(
                mock_git_repo, mock_git_repo, "packages/group/pylib", "pylib",
            )
        )
        assert result.status == "pass"
        assert calls == [
            (["uv", "sync", "--all-packages", "--dry-run"], str(mock_git_repo))
        ], calls

    def test_a_workspace_reached_through_a_symlink_is_still_the_root(
        self, mock_git_repo,
    ):
        """The locator resolves symlinks, so the comparison must too.

        A repository checked out under a symlinked path (a symlinked home, a
        symlinked temp directory) would otherwise stop being recognized as its
        own workspace root and silently switch to the per-project strategy.
        """
        self._package_root(mock_git_repo, ["packages/*"])
        self._member(mock_git_repo, "packages/pylib", "pylib")
        link = mock_git_repo.parent / "linked-repo"
        link.symlink_to(mock_git_repo, target_is_directory=True)

        result, calls = self._calls(
            self._ctx(link, link, "packages/pylib", "pylib")
        )
        assert result.status == "pass"
        assert calls == [
            (["uv", "sync", "--all-packages", "--dry-run"], str(link))
        ], calls


class TestWorkspaceUnbuildablePass:
    """The check passes when uv sync --all-packages --dry-run succeeds."""

    def test_passes_when_sync_succeeds(self, mock_git_repo):
        """uv sync dry-run exits 0 -> pass."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        _make_root_uv_workspace(mock_git_repo, ["mylib"])

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        fake_result = subprocess.CompletedProcess(
            args=["uv", "sync", "--all-packages", "--dry-run"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "pass"
        assert "all workspace members buildable" in result.message


class TestWorkspaceUnbuildableFail:
    """The check fails when uv sync --all-packages --dry-run fails."""

    def test_fails_when_sync_fails(self, mock_git_repo):
        """uv sync dry-run exits non-zero -> fail with details."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        _make_root_uv_workspace(mock_git_repo, ["mylib"])

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        fake_result = subprocess.CompletedProcess(
            args=["uv", "sync", "--all-packages", "--dry-run"],
            returncode=1,
            stdout="",
            stderr="error: Failed to build `broken-pkg`\nCaused by: missing build-system in pyproject.toml",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "fail"
        assert "broken-pkg" in result.message
        assert len(result.problems) == 2

    def test_fails_on_timeout(self, mock_git_repo):
        """uv sync dry-run times out -> fail."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        _make_root_uv_workspace(mock_git_repo, ["mylib"])

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("uv", 120)):
            result = app._check_defs["workspace-unbuildable"].impl(ctx)
        assert result.status == "fail"
        assert "timed out" in result.message


class TestPolyglotNoRootUvWorkspace:
    """A polyglot monorepo with no root pyproject.toml is a real layout.

    Regression for the false positive where the root-level
    ``uv sync --all-packages`` died with "No pyproject.toml found" even though
    every project built fine in its own directory.
    """

    def _ctx(self, root):
        return WorkspaceCheckContext(
            project_root=root,
            workspace_root=root,
            config={},
            projects=[
                {"path": "gotool", "name": "gotool"},
                {"path": "pylib", "name": "pylib"},
            ],
            graph=None,
        )

    def _polyglot(self, root):
        """go project + independent python project, NO root pyproject.toml."""
        go_dir = root / "gotool"
        go_dir.mkdir()
        (go_dir / "go.mod").write_text("module example.com/gotool\n\ngo 1.25\n")
        py_dir = root / "pylib"
        py_dir.mkdir()
        (py_dir / "pyproject.toml").write_text(
            '[project]\nname = "pylib"\nversion = "0.1.0"\n'
        )
        assert not (root / "pyproject.toml").exists()
        return py_dir

    def test_syncs_each_pypi_project_in_its_own_dir(self, mock_git_repo):
        """No root uv workspace -> per-project `uv sync`, and it passes."""
        py_dir = self._polyglot(mock_git_repo)

        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs.get("cwd")))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = app._check_defs["workspace-unbuildable"].impl(self._ctx(mock_git_repo))

        assert result.status == "pass"
        assert calls == [(["uv", "sync", "--dry-run"], str(py_dir))], calls
        assert "--all-packages" not in " ".join(calls[0][0])

    def test_reports_the_failing_project_by_name(self, mock_git_repo):
        """A genuinely unbuildable python project still fails, named."""
        self._polyglot(mock_git_repo)

        fake_result = subprocess.CompletedProcess(
            args=["uv", "sync", "--dry-run"],
            returncode=1,
            stdout="",
            stderr="error: Failed to build `pylib`\nCaused by: missing build-system",
        )
        with patch("subprocess.run", return_value=fake_result):
            result = app._check_defs["workspace-unbuildable"].impl(self._ctx(mock_git_repo))

        assert result.status == "fail"
        assert result.message.startswith("pylib: ")
        assert "Failed to build `pylib`" in result.message
        assert len(result.problems) == 2

    def test_root_uv_workspace_still_syncs_at_the_root(self, mock_git_repo):
        """The marker present -> unchanged root-level `uv sync --all-packages`."""
        self._polyglot(mock_git_repo)
        _make_root_uv_workspace(mock_git_repo, ["pylib"])

        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs.get("cwd")))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = app._check_defs["workspace-unbuildable"].impl(self._ctx(mock_git_repo))

        assert result.status == "pass"
        assert calls == [
            (["uv", "sync", "--all-packages", "--dry-run"], str(mock_git_repo))
        ], calls
