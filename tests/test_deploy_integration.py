"""Tests for deploy integration in the release flow."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import rlsbl.commands.release as _release_mod
from githarness import canned_status_effects_run
from rlsbl.commands.release.execute import ReleaseState
from rlsbl.context import ProjectContext
from rlsbl.deploy import DeployResult
from rlsbl.errors import PostReleaseError
from rlsbl.resolved_target import ResolvedTarget
from rlsbl.targets import TargetEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_expected_refs(self, version, context):
    """The one ref this stand-in target claims: its own primary tag.

    The real targets assemble this from tag_format plus companions and
    recorded aliases; a deploy test only needs the release to have a ref set
    to create.
    """
    from rlsbl.targets.refs import ExpectedRefs

    return ExpectedRefs(version=version, primary=f"v{version}")


_FakeTarget = type("FakeTarget", (), {
    "version_file": lambda self, dir_path=None: None,
    "write_version": lambda self, p, v, ctx=None: [],
    "build": lambda self, p, v, *, config=None: None,
    "tag_format": lambda self, v: f"v{v}",
    "expected_refs": _fake_expected_refs,
})


def _npm_primary_resolved(repo_path, monkeypatch):
    """Register a fake npm registry target and return a primary-marked list.

    Replaces the former ``registry="npm"`` + ``target=FakeTarget()`` scalar
    fields on ReleaseState: the mutating flow now derives the registry name and
    instance from the primary ResolvedTarget and ``TARGETS[name]``.
    """
    monkeypatch.setitem(_release_mod.TARGETS, "npm", _FakeTarget())
    entry = TargetEntry(name="npm", path=str(repo_path))
    return [ResolvedTarget(
        target=entry, path=str(repo_path), pipeline=None,
        publish_mode="ci", artifact_kind=None, primary=True,
    )]


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
    """Deploy targets configured and valid: deploy_target gets called after pipeline dispatch."""

    def test_release_with_deploy_targets(self, mock_git_repo, monkeypatch, capsys):
        deploy_targets = [_minimal_target()]

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
        # The working tree is read through the shared helper on the effects
        # chokepoint, which mock_run never sees: answer it as a clean tree so
        # the fixture's own uncommitted files do not trip the release's
        # concurrent-change guard.
        monkeypatch.setattr("rlsbl.effects.run", canned_status_effects_run())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", lambda args, **kw: "")
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda *a, **kw: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda **k: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(ReleaseState(
            resolved_targets=_npm_primary_resolved(mock_git_repo, monkeypatch),
            flags={"auto-tag": False},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            ctx=ProjectContext(project_root=Path(str(mock_git_repo)), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}, "deploy": deploy_targets}),
        ))

        assert len(deploy_calls) == 1
        assert deploy_calls[0] == ("prod", "main")

        captured = capsys.readouterr()
        assert "Deploying to prod" in captured.out


class TestReleaseDeployFailureContinues:
    """Deploy fails: the release is NOT rolled back and the post-release hook
    still runs, but the run ends in a PostReleaseError.

    "Non-fatal" means the release stays put and stays resumable -- it never
    meant "exit 0". The completion summary is the single decision point: a
    recorded step failure makes the run report itself as failed while keeping
    the state file, so `rlsbl release resume` re-attempts exactly that step.
    """

    def test_release_deploy_failure_continues(self, mock_git_repo, monkeypatch, capsys):
        deploy_targets = [_minimal_target()]

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
        # The working tree is read through the shared helper on the effects
        # chokepoint, which mock_run never sees: answer it as a clean tree so
        # the fixture's own uncommitted files do not trip the release's
        # concurrent-change guard.
        monkeypatch.setattr("rlsbl.effects.run", canned_status_effects_run())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", lambda args, **kw: "")
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda *a, **kw: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda **k: "main")

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

        monkeypatch.setattr("rlsbl.commands.release.effects", FakeSubprocessModule())
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        # Not rolled back, hooks still run -- but the run reports the failure.
        with pytest.raises(PostReleaseError, match="DEPLOYED"):
            _run_release_mutating(ReleaseState(
                resolved_targets=_npm_primary_resolved(mock_git_repo, monkeypatch),
                flags={"auto-tag": False},
                quiet=False,
                log=lambda msg: None,
                new_version="1.0.1",
                current_version="1.0.0",
                bump_type="patch",
                tag="v1.0.1",
                branch="main",
                changelog_entry="- Fixed a bug",
                ctx=ProjectContext(project_root=Path(str(mock_git_repo)), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}, "deploy": deploy_targets}),
            ))

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Retry with: rlsbl deploy prod" in captured.err

        # Post-release hook should have been called via subprocess.run
        assert post_release_hook_ran[0]


class TestReleaseNoDeployConfig:
    """No deploy key in config: release flow skips deploy silently."""

    def test_release_no_deploy_config(self, mock_git_repo, monkeypatch, capsys):
        # Write config without deploy key
        _write_config(mock_git_repo, {"tag": True, "targets": ["npm"]})

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
        # The working tree is read through the shared helper on the effects
        # chokepoint, which mock_run never sees: answer it as a clean tree so
        # the fixture's own uncommitted files do not trip the release's
        # concurrent-change guard.
        monkeypatch.setattr("rlsbl.effects.run", canned_status_effects_run())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", lambda args, **kw: "")
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda *a, **kw: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda **k: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        _run_release_mutating(ReleaseState(
            resolved_targets=_npm_primary_resolved(mock_git_repo, monkeypatch),
            flags={"auto-tag": False},
            quiet=False,
            log=lambda msg: None,
            new_version="1.0.1",
            current_version="1.0.0",
            bump_type="patch",
            tag="v1.0.1",
            branch="main",
            changelog_entry="- Fixed a bug",
            ctx=ProjectContext(project_root=Path(str(mock_git_repo)), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
        ))

        # deploy_target should never have been called
        assert len(deploy_calls) == 0

        captured = capsys.readouterr()
        # No deploy-related output
        assert "Deploying" not in captured.out
        assert "deploy" not in captured.err.lower()


class TestReleaseDeployConfigErrors:
    """Invalid deploy config: deploy skipped, and the run reports the failure.

    A deploy the operator configured and rlsbl refused to attempt is a step
    that did not happen. It is recorded as a DEPLOYED failure, so the run
    exits nonzero instead of announcing a release whose deploy never ran.
    """

    def test_release_deploy_config_errors(self, mock_git_repo, monkeypatch, capsys):
        # Config with invalid deploy target (missing required fields)
        deploy_targets = [{"name": "broken"}]

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
        # The working tree is read through the shared helper on the effects
        # chokepoint, which mock_run never sees: answer it as a clean tree so
        # the fixture's own uncommitted files do not trip the release's
        # concurrent-change guard.
        monkeypatch.setattr("rlsbl.effects.run", canned_status_effects_run())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", lambda args, **kw: "")
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda *a, **kw: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda **k: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        with pytest.raises(PostReleaseError, match="DEPLOYED"):
            _run_release_mutating(ReleaseState(
                resolved_targets=_npm_primary_resolved(mock_git_repo, monkeypatch),
                flags={"auto-tag": False},
                quiet=False,
                log=lambda msg: None,
                new_version="1.0.1",
                current_version="1.0.0",
                bump_type="patch",
                tag="v1.0.1",
                branch="main",
                changelog_entry="- Fixed a bug",
                ctx=ProjectContext(project_root=Path(str(mock_git_repo)), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}, "deploy": deploy_targets}),
            ))

        # deploy_target should never have been called (config has errors)
        assert len(deploy_calls) == 0

        captured = capsys.readouterr()
        assert "deploy config has errors" in captured.err
        assert "missing required field" in captured.err


class TestReleaseStopsAtFirstDeployFailure:
    """Multiple targets: first fails, second not attempted."""

    def test_release_stops_at_first_deploy_failure(self, mock_git_repo, monkeypatch, capsys):
        deploy_targets = [
            _minimal_target(name="staging", only_on=["main"]),
            _minimal_target(name="prod", only_on=["main"]),
        ]

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
        # The working tree is read through the shared helper on the effects
        # chokepoint, which mock_run never sees: answer it as a clean tree so
        # the fixture's own uncommitted files do not trip the release's
        # concurrent-change guard.
        monkeypatch.setattr("rlsbl.effects.run", canned_status_effects_run())
        monkeypatch.setattr("rlsbl.commands.release.run_gh", lambda args, **kw: "")
        monkeypatch.setattr("rlsbl.commands.release.commit_files", lambda msg, files, **kw: True)
        monkeypatch.setattr("rlsbl.commands.release.push_if_needed", lambda b, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.get_push_timeout", lambda *a, **kw: 120)
        monkeypatch.setattr("rlsbl.commands.release.get_current_branch", lambda **k: "main")
        monkeypatch.setattr("rlsbl.commands.release.finalize_version", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_version_file", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.generate_changelog", lambda *a, **kw: None)
        monkeypatch.setattr("rlsbl.commands.release.extract_changelog_entry", lambda *a, **kw: "- Fixed a bug")
        monkeypatch.setattr("rlsbl.commands.release.get_changes_dir", lambda *a, **kw: ".rlsbl/changes")

        with pytest.raises(PostReleaseError, match="DEPLOYED"):
            _run_release_mutating(ReleaseState(
                resolved_targets=_npm_primary_resolved(mock_git_repo, monkeypatch),
                flags={"auto-tag": False},
                quiet=False,
                log=lambda msg: None,
                new_version="1.0.1",
                current_version="1.0.0",
                bump_type="patch",
                tag="v1.0.1",
                branch="main",
                changelog_entry="- Fixed a bug",
                ctx=ProjectContext(project_root=Path(str(mock_git_repo)), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}, "deploy": deploy_targets}),
            ))

        # Only staging was attempted; prod was NOT attempted
        assert deploy_calls == ["staging"]

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Rollback was executed" in captured.err
        assert "Retry with: rlsbl deploy staging" in captured.err
