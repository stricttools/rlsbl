"""Tests for monorepo add with --target flag (plain and explicit targets)."""

import json
import os
from unittest.mock import patch

import pytest

from rlsbl.commands.monorepo import _cmd_init, _cmd_add
from rlsbl.workspace import load_workspace

from conftest import declared_members


class TestAddTargetPlain:
    def test_plain_target_succeeds_on_bare_directory(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "mydir")
        os.makedirs(bare_dir)
        _cmd_add(["mydir"], {"releasable": "false", "target": "plain"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert len(projects) == 1
        assert projects[0]["path"] == "mydir"
        assert projects[0]["name"] == "mydir"
        captured = capsys.readouterr()
        assert "Added project 'mydir' at mydir" in captured.out

    def test_plain_target_passes_target_to_scaffold(self, mock_git_repo, capsys):
        """Scaffold subprocess receives --target plain when explicit target is given."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "mydir")
        os.makedirs(bare_dir)
        subprocess_calls = []

        original_run = __import__("subprocess").run

        def capture_run(cmd, *args, **kwargs):
            subprocess_calls.append(cmd)
            # Let safegit/git calls through, stub rlsbl scaffold
            if isinstance(cmd, list) and "rlsbl" in " ".join(cmd):
                return __import__("subprocess").CompletedProcess(cmd, 0)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=capture_run), \
             patch("rlsbl.commands.monorepo.commands.commit_files", return_value=True):
            _cmd_add(["mydir"], {"releasable": "false", "target": "plain"}, project_root=".")

        scaffold_calls = [
            c for c in subprocess_calls
            if isinstance(c, list) and "scaffold" in c
        ]
        assert len(scaffold_calls) >= 1
        cmd = scaffold_calls[0]
        assert "--target" in cmd
        assert "plain" in cmd

    def test_plain_target_does_not_create_version_directly(self, mock_git_repo, capsys):
        """_cmd_add no longer creates VERSION directly; scaffold handles it."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "mydir")
        os.makedirs(bare_dir)

        # Stub the scaffold subprocess out, so what remains is exactly what
        # _cmd_add itself wrote. Letting the real scaffold run would make the
        # absence of VERSION depend on whether that subprocess succeeded, which
        # is why this assertion was missing.
        original_run = __import__("subprocess").run

        def stub_scaffold(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "rlsbl" in " ".join(cmd):
                return __import__("subprocess").CompletedProcess(cmd, 0)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=stub_scaffold), \
             patch("rlsbl.commands.monorepo.commands.commit_files", return_value=True):
            _cmd_add(["mydir"], {"releasable": "false", "target": "plain"}, project_root=".")

        version_path = os.path.join(str(mock_git_repo), "mydir", "VERSION")
        assert not os.path.exists(version_path)
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert len(projects) == 1

    def test_plain_project_in_workspace(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "mydir")
        os.makedirs(bare_dir)
        _cmd_add(["mydir"], {"releasable": "false", "target": "plain"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert len(projects) == 1
        assert projects[0]["name"] == "mydir"
        assert projects[0]["path"] == "mydir"

    def test_bare_directory_without_target_flag_errors(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "empty-dir")
        os.makedirs(bare_dir)
        with pytest.raises(SystemExit):
            _cmd_add(["empty-dir"], {"releasable": "false"}, project_root=".")
        captured = capsys.readouterr()
        assert "No release target detected" in captured.err


class TestAddExplicitTarget:
    def test_explicit_npm_target_skips_auto_detection(self, mock_git_repo, capsys):
        """--target npm works on a directory with package.json."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        proj_dir = os.path.join(str(mock_git_repo), "mypkg")
        os.makedirs(proj_dir)
        with open(os.path.join(proj_dir, "package.json"), "w") as f:
            json.dump({"name": "mypkg", "version": "1.0.0"}, f)
        _cmd_add(["mypkg"], {"releasable": "false", "target": "npm"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert len(projects) == 1
        assert projects[0]["name"] == "mypkg"

    def test_unknown_target_errors(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        proj_dir = os.path.join(str(mock_git_repo), "mydir")
        os.makedirs(proj_dir)
        with pytest.raises(SystemExit):
            _cmd_add(["mydir"], {"releasable": "false", "target": "nonexistent"}, project_root=".")
        captured = capsys.readouterr()
        assert "Unknown target" in captured.err

    def test_plain_target_with_name_flag(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "libs/docs")
        os.makedirs(bare_dir, exist_ok=True)
        _cmd_add(["libs/docs"], {"releasable": "false", "target": "plain", "name": "my-docs"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert projects[0]["name"] == "my-docs"


class TestAddRegistryName:
    def test_registry_name_written_to_workspace(self, mock_git_repo, capsys):
        """--registry-name persists registry_name in the workspace project entry."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "core")
        os.makedirs(bare_dir)
        _cmd_add(["core"], {"releasable": "false", "target": "plain", "registry-name": "my-registry-id"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert projects[0]["name"] == "core"
        assert projects[0].registry_name == "my-registry-id"

    def test_no_registry_name_omits_field(self, mock_git_repo, capsys):
        """Without --registry-name, the field is absent (empty default)."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "core")
        os.makedirs(bare_dir)
        _cmd_add(["core"], {"releasable": "false", "target": "plain"}, project_root=".")
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert "registry_name" not in projects[0]


class TestAddDryRun:
    def test_dry_run_makes_zero_mutations(self, mock_git_repo, capsys):
        """--dry-run validates and reports but does not mutate the workspace."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        bare_dir = os.path.join(str(mock_git_repo), "core")
        os.makedirs(bare_dir)
        capsys.readouterr()

        subprocess_calls = []
        original_run = __import__("subprocess").run

        def capture_run(cmd, *args, **kwargs):
            subprocess_calls.append(cmd)
            if isinstance(cmd, list) and "rlsbl" in " ".join(str(c) for c in cmd):
                return __import__("subprocess").CompletedProcess(cmd, 0)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=capture_run):
            _cmd_add(["core"], {"releasable": "false", "target": "plain"}, project_root=".", dry_run=True)

        # Workspace unchanged: project NOT added.
        projects = declared_members(load_workspace(str(mock_git_repo)))
        assert len(projects) == 0

        # No scaffold or sync subprocess ran.
        rlsbl_calls = [
            c for c in subprocess_calls
            if isinstance(c, list) and ("scaffold" in c or "sync" in c)
        ]
        assert rlsbl_calls == []

        # Would-do report printed.
        captured = capsys.readouterr()
        assert "Would add" in captured.out or "would add" in captured.out.lower()

    def test_dry_run_still_validates(self, mock_git_repo, capsys):
        """--dry-run runs full validation: a missing directory still errors."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["does-not-exist"], {"releasable": "false", "target": "plain"}, project_root=".", dry_run=True)

    def test_dry_run_validates_name_uniqueness(self, mock_git_repo, capsys):
        """--dry-run rejects a duplicate name just like a real add."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        os.makedirs(os.path.join(str(mock_git_repo), "core"))
        os.makedirs(os.path.join(str(mock_git_repo), "core2"))
        _cmd_add(["core"], {"releasable": "false", "target": "plain"}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["core2"], {"releasable": "false", "target": "plain", "name": "core"}, project_root=".", dry_run=True)
        captured = capsys.readouterr()
        assert "already exists" in captured.err
