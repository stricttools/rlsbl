"""Tests for rlsbl.commands.deploy_cmd — the deploy command."""

import json
from pathlib import Path

import pytest

from rlsbl.commands.deploy_cmd import run_cmd
from rlsbl.context import ProjectContext
from rlsbl.deploy import DeployResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_target(**overrides):
    """Return a minimal valid deploy target dict, with optional overrides."""
    base = {
        "name": "prod",
        "host": "10.0.0.1",
        "steps": ["systemctl restart app"],
        "only_on": ["main"],
    }
    base.update(overrides)
    return base


def _write_deploy_config(mock_git_repo, targets):
    """Write a .rlsbl/config.json with the given deploy targets."""
    config_dir = mock_git_repo / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    config = {"deploy": targets}
    (config_dir / "config.json").write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeployNoConfig:

    def test_deploy_no_config(self, mock_git_repo, capsys):
        """No deploy key in config -> error message."""
        config_dir = mock_git_repo / ".rlsbl"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({}))

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={}))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No deploy targets configured" in captured.err


class TestDeployAutoSelectSingleTarget:

    def test_deploy_auto_select_single_target(self, mock_git_repo, monkeypatch, capsys):
        """One target configured, no name arg -> auto-selects."""
        targets = [_minimal_target()]

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append((target_config["name"], current_branch))
            return DeployResult("prod", True, "Deploy completed")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda: "main")

        run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert len(deploy_calls) == 1
        assert deploy_calls[0][0] == "prod"


class TestDeploySelectByName:

    def test_deploy_select_by_name(self, mock_git_repo, monkeypatch, capsys):
        """Multiple targets, name arg -> selects correct one."""
        targets = [
            _minimal_target(name="staging", only_on=["main", "develop"]),
            _minimal_target(name="prod"),
        ]

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult(target_config["name"], True, "Deploy completed")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda: "main")

        run_cmd(None, ["staging"], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert len(deploy_calls) == 1
        assert deploy_calls[0] == "staging"


class TestDeployAmbiguousNoName:

    def test_deploy_ambiguous_no_name(self, mock_git_repo, capsys):
        """Multiple targets, no name -> error listing names."""
        targets = [
            _minimal_target(name="staging"),
            _minimal_target(name="prod"),
        ]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "multiple deploy targets" in captured.err.lower()
        assert "staging" in captured.err
        assert "prod" in captured.err


class TestDeployUnknownName:

    def test_deploy_unknown_name(self, mock_git_repo, capsys):
        """Name arg doesn't match any target -> error."""
        targets = [_minimal_target(name="prod")]

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, ["nonexistent"], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "unknown deploy target" in captured.err.lower()
        assert "nonexistent" in captured.err
        assert "prod" in captured.err


class TestDeployDryRun:

    def test_deploy_dry_run(self, mock_git_repo, monkeypatch, capsys):
        """--dry-run flag -> prints info, no SSH calls."""
        target = _minimal_target(
            directory="/opt/app",
            health={"type": "http", "url": "http://localhost/health"},
        )

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult("prod", True, "Deploy completed")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"dry-run": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": [target]}))

        assert exc_info.value.code == 0
        # deploy_target should NOT have been called
        assert len(deploy_calls) == 0

        captured = capsys.readouterr()
        assert "prod" in captured.out
        assert "10.0.0.1" in captured.out
        assert "systemctl restart app" in captured.out
        assert "/opt/app" in captured.out
        assert "http" in captured.out
        assert "dry run" in captured.out.lower()


class TestDeployBranchRestriction:

    def test_deploy_branch_restriction(self, mock_git_repo, monkeypatch, capsys):
        """Wrong branch without --force -> error."""
        targets = [_minimal_target(only_on=["production"])]

        # Current branch is "main" (from mock_git_repo), not "production"
        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult("prod", True, "Deploy completed")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert exc_info.value.code == 1
        assert len(deploy_calls) == 0
        captured = capsys.readouterr()
        assert "--force" in captured.err
        assert "production" in captured.err


class TestDeployForceOverridesBranch:

    def test_deploy_force_overrides_branch(self, mock_git_repo, monkeypatch, capsys):
        """Wrong branch with --force -> deploys anyway."""
        targets = [_minimal_target(only_on=["production"])]

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult("prod", True, "Deploy completed")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)

        run_cmd(None, [], {"force": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert len(deploy_calls) == 1


class TestDeploySuccess:

    def test_deploy_success(self, mock_git_repo, monkeypatch, capsys):
        """Happy path with mocked deploy_target."""
        targets = [_minimal_target()]

        def mock_deploy_target(target_config, current_branch):
            return DeployResult("prod", True, "All steps passed, health OK")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda: "main")

        run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        captured = capsys.readouterr()
        assert "[prod]" in captured.out
        assert "All steps passed" in captured.out


class TestDeployFailure:

    def test_deploy_failure(self, mock_git_repo, monkeypatch, capsys):
        """Step fails -> exit 1."""
        targets = [_minimal_target()]

        def mock_deploy_target(target_config, current_branch):
            return DeployResult("prod", False, "Step 1 failed (exit 1): connection refused")

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy_target)
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda: "main")

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Deploy failed" in captured.err
        assert "connection refused" in captured.err
