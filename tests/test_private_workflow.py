"""Tests for private repository workflow: --private flag, publish skip, asset upload hook."""

import json
import os
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.commands.init_cmd import (
    _resolve_private,
    _filter_mappings_for_private,
    process_mappings,
    run_cmd,
)
from rlsbl.config import read_project_config
from rlsbl.utils import is_private_repo


class TestIsPrivateRepo:
    """Tests for is_private_repo() utility function."""

    def test_private_repo_returns_true(self, monkeypatch):
        """Mock GitHub API returning private=true, verify True returned."""
        monkeypatch.setattr(
            "rlsbl.utils.run",
            lambda cmd, args, **kw: {
                ("git", ("remote", "get-url", "origin")): "git@github.com:owner/repo.git",
                ("gh", ("auth", "token")): "fake-token",
            }[(cmd, tuple(args))],
        )

        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"private": True}).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: fake_resp)

        assert is_private_repo() is True

    def test_public_repo_returns_false(self, monkeypatch):
        """Mock GitHub API returning private=false, verify False returned."""
        monkeypatch.setattr(
            "rlsbl.utils.run",
            lambda cmd, args, **kw: {
                ("git", ("remote", "get-url", "origin")): "https://github.com/owner/repo",
                ("gh", ("auth", "token")): "fake-token",
            }[(cmd, tuple(args))],
        )

        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"private": False}).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: fake_resp)

        assert is_private_repo() is False

    def test_failure_returns_none(self, monkeypatch):
        """When git/gh commands fail, verify None is returned."""
        def failing_run(cmd, args, **kw):
            raise Exception("command not found")

        monkeypatch.setattr("rlsbl.utils.run", failing_run)

        assert is_private_repo() is None

    def test_non_github_remote_returns_none(self, monkeypatch):
        """When remote is not github.com, return None."""
        monkeypatch.setattr(
            "rlsbl.utils.run",
            lambda cmd, args, **kw: "git@gitlab.com:owner/repo.git"
            if cmd == "git" else "fake-token",
        )

        assert is_private_repo() is None


class TestPrivateFlagScaffold:
    """Tests for --private flag integration with scaffold."""

    def test_private_flag_skips_publish_template(self, mock_git_repo):
        """Scaffold with private=True should not create publish.yml."""
        # Create a package.json so npm target is detected
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=True):
                run_cmd("npm", [], {"private": True, "no-tag": True})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert not os.path.exists(publish_path), "publish.yml should not exist for private repos"

        # CI workflow should still exist
        ci_path = os.path.join(".github", "workflows", "ci.yml")
        assert os.path.exists(ci_path), "ci.yml should still be created"

    def test_private_auto_detection(self, mock_git_repo):
        """When is_private_repo() returns True, scaffold should skip publish."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=True):
                run_cmd("npm", [], {"no-tag": True})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert not os.path.exists(publish_path), "publish.yml should not exist for auto-detected private repos"

    def test_private_writes_standard_post_release_hook(self, mock_git_repo):
        """Private scaffold should write the standard post-release hook (asset upload is built-in)."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=True):
                run_cmd("npm", [], {"private": True, "no-tag": True})

        hook_path = os.path.join(".rlsbl", "hooks", "post-release.sh")
        assert os.path.exists(hook_path)

        with open(hook_path) as f:
            content = f.read()

        # Should NOT contain legacy private hook content -- asset upload is now built-in
        assert "gh release upload" not in content
        assert "Post-release hook for private repositories" not in content

    def test_private_saved_to_config(self, mock_git_repo):
        """Private flag should be saved to .rlsbl/config.json."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=True):
                run_cmd("npm", [], {"private": True, "no-tag": True})

        config = read_project_config()
        assert config.get("private") is True

    def test_public_repo_creates_publish(self, mock_git_repo):
        """Public repos should still get publish.yml."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=False):
                run_cmd("npm", [], {"no-tag": True})

        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert os.path.exists(publish_path), "publish.yml should exist for public repos"

    def test_private_config_remembered(self, mock_git_repo):
        """Once private is saved in config, subsequent scaffolds remember it."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        # First scaffold with --private
        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=None):
                run_cmd("npm", [], {"private": True, "no-tag": True})

        # Remove publish.yml tracking and CI to force re-creation scenario
        publish_path = os.path.join(".github", "workflows", "publish.yml")
        assert not os.path.exists(publish_path)

        # Second scaffold without --private flag, but config remembers
        with patch("sys.stdout", new_callable=StringIO):
            with patch("rlsbl.commands.init_cmd.is_private_repo", return_value=None):
                run_cmd("npm", [], {"force": True, "no-tag": True})

        assert not os.path.exists(publish_path), "publish.yml should still not exist (config remembers private)"


class TestFilterMappings:
    """Unit tests for mapping filter helpers."""

    def test_filter_removes_publish_templates(self):
        """_filter_mappings_for_private removes any mapping with 'publish' in template."""
        mappings = [
            {"template": "ci.yml.tpl", "target": ".github/workflows/ci.yml"},
            {"template": "publish.yml.tpl", "target": ".github/workflows/publish.yml"},
            {"template": "goreleaser.yml.tpl", "target": ".goreleaser.yml"},
        ]
        result = _filter_mappings_for_private(mappings)
        assert len(result) == 2
        templates = [m["template"] for m in result]
        assert "publish.yml.tpl" not in templates
        assert "ci.yml.tpl" in templates
        assert "goreleaser.yml.tpl" in templates

    def test_replace_function_removed(self):
        """_replace_post_release_hook_for_private no longer exists."""
        import rlsbl.commands.init_cmd as init_cmd
        assert not hasattr(init_cmd, "_replace_post_release_hook_for_private")
