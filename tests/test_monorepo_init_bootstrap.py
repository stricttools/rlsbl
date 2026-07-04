"""Tests for monorepo init bootstrap fix.

rlsbl monorepo init should work in directories without a pre-existing
.rlsbl/ marker (bootstrap). It should refuse if CWD is inside an
existing workspace or if workspace.toml already exists.
"""

import os
import subprocess

import pytest

from rlsbl.workspace import (
    find_workspace_root,
    save_workspace,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
)
from rlsbl.commands.monorepo.commands import _cmd_init


class TestMonorepoInitBootstrap:
    """monorepo init resolves to CWD without requiring .rlsbl/ marker."""

    def test_init_in_fresh_repo_succeeds(self, tmp_path, monkeypatch):
        """monorepo init works in a directory with no .rlsbl/."""
        # Set up a git repo with no .rlsbl/ directory
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("# test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

        monkeypatch.chdir(tmp_path)

        assert not (tmp_path / ".rlsbl").exists()
        assert not (tmp_path / WORKSPACE_DIR).exists()

        # _cmd_init should create the workspace
        _cmd_init({"auto-commit": False}, project_root=tmp_path)

        # workspace.toml should now exist
        ws_file = tmp_path / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()

    def test_init_refuses_if_workspace_exists(self, tmp_path, monkeypatch):
        """monorepo init errors when workspace.toml already exists."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("# test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

        monkeypatch.chdir(tmp_path)

        # Create workspace first
        save_workspace(str(tmp_path), [])

        with pytest.raises(SystemExit):
            _cmd_init({"auto-commit": False}, project_root=tmp_path)

    def test_init_from_subdir_of_standalone_project_errors(self, tmp_path, monkeypatch):
        """monorepo init from a subdir of a standalone .rlsbl/ project errors."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("# test\n")
        # Create a standalone .rlsbl/ marker at root
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / ".rlsbl" / "config.json").write_text("{}")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

        sub_dir = tmp_path / "packages" / "core"
        sub_dir.mkdir(parents=True)
        monkeypatch.chdir(sub_dir)

        from rlsbl import cmd_mono_init
        with pytest.raises(SystemExit):
            cmd_mono_init(auto_commit=False)

    def test_init_at_standalone_project_root_succeeds(self, tmp_path, monkeypatch):
        """monorepo init at a project root with .rlsbl/ succeeds (conversion case)."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("# test\n")
        # Create a standalone .rlsbl/ marker at root
        (tmp_path / ".rlsbl").mkdir()
        (tmp_path / ".rlsbl" / "config.json").write_text("{}")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

        monkeypatch.chdir(tmp_path)

        # _cmd_init should work -- converting standalone to monorepo
        _cmd_init({"auto-commit": False}, project_root=tmp_path)

        ws_file = tmp_path / WORKSPACE_DIR / WORKSPACE_FILE
        assert ws_file.exists()

    def test_nested_workspace_guard(self, tmp_path):
        """find_workspace_root detects an existing workspace above CWD."""
        # Create a workspace at root
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        save_workspace(str(tmp_path), [])

        # CWD inside the workspace
        sub_dir = tmp_path / "packages" / "core"
        sub_dir.mkdir(parents=True)

        result = find_workspace_root(str(sub_dir))
        assert result is not None
        assert str(tmp_path) in result
