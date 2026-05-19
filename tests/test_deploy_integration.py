"""Tests for deploy integration in the release flow."""

import json
import sys
from unittest.mock import patch

import pytest

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


def _write_deploy_config(repo_path, targets):
    """Write a .rlsbl/config.json with the given deploy targets."""
    config_dir = repo_path / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    config = {"deploy": targets}
    (config_dir / "config.json").write_text(json.dumps(config))


def _write_config(repo_path, config):
    """Write a .rlsbl/config.json with arbitrary content."""
    config_dir = repo_path / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReleaseWithDeployTargets:
    """Deploy targets configured and valid: deploy_target gets called after publish."""

    def test_release_with_deploy_targets(self, mock_git_repo, monkeypatch, capsys):
        _write_deploy_config(mock_git_repo, [_minimal_target()])

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append((target_config["name"], current_branch))
            return DeployResult("prod", True, "Deploy completed (no health check)")

        monkeypatch.setattr("rlsbl.commands.release.deploy_target", mock_deploy_target)

        # Import after patching
        from rlsbl.commands.release import _run_release_mutating

        # Mock all the git/gh operations that _run_release_mutating performs
        run_calls = []

        def mock_run(cmd, args, **kwargs):
            run_calls.append((cmd, args))
            if cmd == "git" and args and args[0] == "status":
                return ""
            if cmd == "git" and args and args[0] == "diff":
                return ""
            if cmd == "git" and args and args[0] == "rev-parse":
                return "abc123"
            return ""

        monkeypatch.setattr("rlsbl.commands.release.run", mock_run)
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(
            registry="npm",
            reg=type("FakeReg", (), {
                "version_file": lambda self: None,
                "write_version": lambda self, p, v: [],
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
                "tag_format": lambda self, v: f"v{v}",
            })(),
            flags={"yes": True, "no-tag": True},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            target=type("FakeTarget", (), {
                "tag_format": lambda self, v: f"v{v}",
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
            })(),
        )

        assert len(deploy_calls) == 1
        assert deploy_calls[0] == ("prod", "main")

        captured = capsys.readouterr()
        assert "Deploying to prod" in captured.out


class TestReleaseDeployFailureContinues:
    """Deploy fails but release still completes (post-release hook runs)."""

    def test_release_deploy_failure_continues(self, mock_git_repo, monkeypatch, capsys):
        _write_deploy_config(mock_git_repo, [_minimal_target()])

        def mock_deploy_target(target_config, current_branch):
            return DeployResult("prod", False, "Step 1 failed (exit 1): connection refused")

        monkeypatch.setattr("rlsbl.commands.release.deploy_target", mock_deploy_target)

        from rlsbl.commands.release import _run_release_mutating

        run_calls = []

        def mock_run(cmd, args, **kwargs):
            run_calls.append((cmd, args))
            if cmd == "git" and args and args[0] == "status":
                return ""
            if cmd == "git" and args and args[0] == "diff":
                return ""
            if cmd == "git" and args and args[0] == "rev-parse":
                return "abc123"
            return ""

        monkeypatch.setattr("rlsbl.commands.release.run", mock_run)
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda: "main")

        # Create a fake post-release hook to verify it still runs after deploy failure
        import os
        import subprocess as sp
        hooks_dir = mock_git_repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "post-release.sh"
        hook_path.write_text("#!/bin/bash\necho post-release\n")
        hook_path.chmod(0o755)

        # Track subprocess.run calls to verify the post-release hook ran
        post_release_hook_ran = [False]
        import rlsbl.commands.release as release_mod
        original_sp = release_mod.subprocess

        class FakeSubprocessModule:
            CalledProcessError = sp.CalledProcessError
            TimeoutExpired = sp.TimeoutExpired

            @staticmethod
            def run(cmd, **kwargs):
                if len(cmd) > 1 and "post-release" in cmd[1]:
                    post_release_hook_ran[0] = True
                return sp.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr("rlsbl.commands.release.subprocess", FakeSubprocessModule())
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        # Should NOT raise -- deploy failure is non-fatal
        _run_release_mutating(
            registry="npm",
            reg=type("FakeReg", (), {
                "version_file": lambda self: None,
                "write_version": lambda self, p, v: [],
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
                "tag_format": lambda self, v: f"v{v}",
            })(),
            flags={"yes": True, "no-tag": True},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            target=type("FakeTarget", (), {
                "tag_format": lambda self, v: f"v{v}",
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
            })(),
        )

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Retry with: rlsbl deploy prod" in captured.err

        # Post-release hook should have been called via subprocess.run
        assert post_release_hook_ran[0]


class TestReleaseNoDeployConfig:
    """No deploy key in config: release flow skips deploy silently."""

    def test_release_no_deploy_config(self, mock_git_repo, monkeypatch, capsys):
        # Write config without deploy key
        _write_config(mock_git_repo, {"tag": True})

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult("prod", True, "ok")

        monkeypatch.setattr("rlsbl.commands.release.deploy_target", mock_deploy_target)

        from rlsbl.commands.release import _run_release_mutating

        def mock_run(cmd, args, **kwargs):
            if cmd == "git" and args and args[0] == "status":
                return ""
            if cmd == "git" and args and args[0] == "diff":
                return ""
            if cmd == "git" and args and args[0] == "rev-parse":
                return "abc123"
            return ""

        monkeypatch.setattr("rlsbl.commands.release.run", mock_run)
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(
            registry="npm",
            reg=type("FakeReg", (), {
                "version_file": lambda self: None,
                "write_version": lambda self, p, v: [],
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
                "tag_format": lambda self, v: f"v{v}",
            })(),
            flags={"yes": True, "no-tag": True},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            target=type("FakeTarget", (), {
                "tag_format": lambda self, v: f"v{v}",
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
            })(),
        )

        # deploy_target should never have been called
        assert len(deploy_calls) == 0

        captured = capsys.readouterr()
        # No deploy-related output
        assert "Deploying" not in captured.out
        assert "deploy" not in captured.err.lower()


class TestReleaseDeployConfigErrors:
    """Invalid deploy config: warning printed, deploy skipped."""

    def test_release_deploy_config_errors(self, mock_git_repo, monkeypatch, capsys):
        # Write config with invalid deploy target (missing required fields)
        _write_config(mock_git_repo, {"deploy": [{"name": "broken"}]})

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            return DeployResult("broken", True, "ok")

        monkeypatch.setattr("rlsbl.commands.release.deploy_target", mock_deploy_target)

        from rlsbl.commands.release import _run_release_mutating

        def mock_run(cmd, args, **kwargs):
            if cmd == "git" and args and args[0] == "status":
                return ""
            if cmd == "git" and args and args[0] == "diff":
                return ""
            if cmd == "git" and args and args[0] == "rev-parse":
                return "abc123"
            return ""

        monkeypatch.setattr("rlsbl.commands.release.run", mock_run)
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(
            registry="npm",
            reg=type("FakeReg", (), {
                "version_file": lambda self: None,
                "write_version": lambda self, p, v: [],
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
                "tag_format": lambda self, v: f"v{v}",
            })(),
            flags={"yes": True, "no-tag": True},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            target=type("FakeTarget", (), {
                "tag_format": lambda self, v: f"v{v}",
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
            })(),
        )

        # deploy_target should never have been called (config has errors)
        assert len(deploy_calls) == 0

        captured = capsys.readouterr()
        assert "deploy config has errors" in captured.err
        assert "missing required field" in captured.err


class TestReleaseStopsAtFirstDeployFailure:
    """Multiple targets: first fails, second not attempted."""

    def test_release_stops_at_first_deploy_failure(self, mock_git_repo, monkeypatch, capsys):
        targets = [
            _minimal_target(name="staging", only_on=["main"]),
            _minimal_target(name="prod", only_on=["main"]),
        ]
        _write_deploy_config(mock_git_repo, targets)

        deploy_calls = []

        def mock_deploy_target(target_config, current_branch):
            deploy_calls.append(target_config["name"])
            if target_config["name"] == "staging":
                return DeployResult("staging", False, "SSH connection refused", rolled_back=True)
            return DeployResult(target_config["name"], True, "ok")

        monkeypatch.setattr("rlsbl.commands.release.deploy_target", mock_deploy_target)

        from rlsbl.commands.release import _run_release_mutating

        def mock_run(cmd, args, **kwargs):
            if cmd == "git" and args and args[0] == "status":
                return ""
            if cmd == "git" and args and args[0] == "diff":
                return ""
            if cmd == "git" and args and args[0] == "rev-parse":
                return "abc123"
            return ""

        monkeypatch.setattr("rlsbl.commands.release.run", mock_run)
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(
            registry="npm",
            reg=type("FakeReg", (), {
                "version_file": lambda self: None,
                "write_version": lambda self, p, v: [],
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
                "tag_format": lambda self, v: f"v{v}",
            })(),
            flags={"yes": True, "no-tag": True},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            target=type("FakeTarget", (), {
                "tag_format": lambda self, v: f"v{v}",
                "build": lambda self, p, v: None,
                "publish": lambda self, p, v: None,
            })(),
        )

        # Only staging was attempted; prod was NOT attempted
        assert deploy_calls == ["staging"]

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Rollback was executed" in captured.err
        assert "Retry with: rlsbl deploy staging" in captured.err
