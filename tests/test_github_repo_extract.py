"""Tests for extract_github_repo_from_remote, get_origin_repo, get_github_repo, and run_gh."""

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from rlsbl.utils import extract_github_repo_from_remote, get_origin_repo, get_github_repo, run_gh


class TestExtractGithubRepoFromRemote:
    def test_https_with_git_suffix(self):
        assert extract_github_repo_from_remote("https://github.com/smm-h/rlsbl.git") == "smm-h/rlsbl"

    def test_https_without_git_suffix(self):
        assert extract_github_repo_from_remote("https://github.com/smm-h/rlsbl") == "smm-h/rlsbl"

    def test_scp_github(self):
        assert extract_github_repo_from_remote("git@github.com:smm-h/rlsbl.git") == "smm-h/rlsbl"

    def test_scp_ssh_alias(self):
        assert extract_github_repo_from_remote("git@gw:GreenCapitals/shopkeep.git") == "GreenCapitals/shopkeep"

    def test_scp_bare_alias(self):
        assert extract_github_repo_from_remote("gp:smm-h/rlsbl.git") == "smm-h/rlsbl"

    def test_scp_without_git_suffix(self):
        assert extract_github_repo_from_remote("git@gp:smm-h/rlsbl") == "smm-h/rlsbl"

    def test_invalid_url(self):
        assert extract_github_repo_from_remote("not-a-url") is None

    def test_empty_string(self):
        assert extract_github_repo_from_remote("") is None

    def test_local_path(self):
        assert extract_github_repo_from_remote("/home/user/repo") is None


class TestGetOriginRepo:
    def test_returns_parsed_repo(self):
        with patch("rlsbl.utils.run", return_value="git@github.com:smm-h/rlsbl.git"):
            assert get_origin_repo() == "smm-h/rlsbl"

    def test_returns_none_on_error(self):
        with patch("rlsbl.utils.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert get_origin_repo() is None

    def test_returns_none_on_missing_git(self):
        with patch("rlsbl.utils.run", side_effect=FileNotFoundError):
            assert get_origin_repo() is None


class TestGetGithubRepo:
    def test_config_key_set(self):
        """Config github_repo takes precedence over remote parsing."""
        result = get_github_repo({"github_repo": "acme/widgets"})
        assert result == "acme/widgets"

    def test_config_none_falls_back_to_remote(self):
        """config=None falls back to get_origin_repo."""
        with patch("rlsbl.utils.get_origin_repo", return_value="smm-h/rlsbl"):
            assert get_github_repo(None) == "smm-h/rlsbl"

    def test_config_without_key_falls_back_to_remote(self):
        """Config dict without github_repo key falls back to remote."""
        with patch("rlsbl.utils.get_origin_repo", return_value="smm-h/rlsbl"):
            assert get_github_repo({"other_key": "value"}) == "smm-h/rlsbl"

    def test_config_empty_string_falls_back(self):
        """Empty string in config is falsy, so falls back to remote."""
        with patch("rlsbl.utils.get_origin_repo", return_value="smm-h/rlsbl"):
            assert get_github_repo({"github_repo": ""}) == "smm-h/rlsbl"

    def test_no_config_no_remote(self):
        """Both sources fail: returns None."""
        with patch("rlsbl.utils.get_origin_repo", return_value=None):
            assert get_github_repo(None) is None


class TestRunGh:
    def test_sets_gh_repo_when_resolvable(self):
        """run_gh sets GH_REPO in subprocess env when repo is resolved."""
        with patch("rlsbl.utils.get_github_repo", return_value="smm-h/rlsbl"), \
             patch("rlsbl.utils.run") as mock_run:
            mock_run.return_value = "ok"
            result = run_gh(["release", "list"], config={"github_repo": "smm-h/rlsbl"})
            assert result == "ok"
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0] == ("gh", ["release", "list"])
            env = call_args[1]["env"]
            assert env["GH_REPO"] == "smm-h/rlsbl"

    def test_no_gh_repo_when_unresolvable(self):
        """run_gh does not set GH_REPO when repo cannot be resolved."""
        with patch("rlsbl.utils.get_github_repo", return_value=None), \
             patch("rlsbl.utils.run") as mock_run:
            mock_run.return_value = "ok"
            result = run_gh(["auth", "status"], config=None)
            assert result == "ok"
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0] == ("gh", ["auth", "status"])
            # env should not be in kwargs (or at least GH_REPO not set)
            env = call_args[1].get("env")
            assert env is None or "GH_REPO" not in env

    def test_does_not_mutate_os_environ(self):
        """run_gh must not modify os.environ (thread-safety)."""
        original_environ = os.environ.copy()
        with patch("rlsbl.utils.get_github_repo", return_value="smm-h/rlsbl"), \
             patch("rlsbl.utils.run") as mock_run:
            mock_run.return_value = "ok"
            run_gh(["release", "list"])
            assert "GH_REPO" not in os.environ
            assert os.environ == original_environ

    def test_forwards_extra_kwargs(self):
        """run_gh forwards timeout, cwd, and other kwargs to run."""
        with patch("rlsbl.utils.get_github_repo", return_value="smm-h/rlsbl"), \
             patch("rlsbl.utils.run") as mock_run:
            mock_run.return_value = "ok"
            run_gh(["release", "list"], config=None, timeout=30, cwd="/tmp")
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 30
            assert call_kwargs["cwd"] == "/tmp"

    def test_caller_env_merged_with_gh_repo(self):
        """When caller passes env=, GH_REPO is added to that env, not os.environ."""
        caller_env = {"PATH": "/usr/bin", "CUSTOM": "value"}
        with patch("rlsbl.utils.get_github_repo", return_value="acme/proj"), \
             patch("rlsbl.utils.run") as mock_run:
            mock_run.return_value = "ok"
            run_gh(["release", "list"], env=caller_env)
            call_env = mock_run.call_args[1]["env"]
            assert call_env["GH_REPO"] == "acme/proj"
            assert call_env["PATH"] == "/usr/bin"
            assert call_env["CUSTOM"] == "value"
            # Original caller_env must not be mutated either
            assert "GH_REPO" not in caller_env
