"""Targeted tests to increase code coverage for low-coverage modules.

Covers: discover.py, mirror_cmd.py, record_gif.py, prs.py, migrate.py,
edit_release.py, claim_name.py, deploy_cmd.py, changelog/files.py,
changelog/resolve.py, release_retry.py.
"""

import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from conftest import FakeResponse, run_git, git_head, make_commit


# ============================================================================
# discover.py
# ============================================================================

from rlsbl.commands.discover import (
    _get_github_token,
    _make_request,
    _parse_next_link,
    _relative_time,
    _get_authenticated_user,
    _fetch_all_repos,
    run_cmd as discover_run_cmd,
)


class TestGetGithubToken:
    """Tests for _get_github_token."""

    def test_returns_env_token_when_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token-123")
        assert _get_github_token() == "env-token-123"

    def test_falls_back_to_gh_cli(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_result = MagicMock()
        mock_result.stdout = "gh-cli-token-456\n"
        with patch("subprocess.run", return_value=mock_result):
            assert _get_github_token() == "gh-cli-token-456"

    def test_returns_none_when_gh_cli_returns_empty(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_result = MagicMock()
        mock_result.stdout = "\n"
        with patch("subprocess.run", return_value=mock_result):
            assert _get_github_token() is None

    def test_returns_none_when_gh_cli_fails(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("subprocess.run", side_effect=FileNotFoundError("no gh")):
            assert _get_github_token() is None


class TestMakeRequestNoToken:
    """Tests for _make_request without a token."""

    def test_request_without_token(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            # Verify no Authorization header set
            assert "Authorization" not in dict(req.headers)
            return FakeResponse({"result": "ok"}, headers={"X-Test": "yes"})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        data, headers = _make_request("https://api.github.com/test", None)
        assert data == {"result": "ok"}


class TestMakeRequestRetryAfterCap:
    """Test that Retry-After is capped at 10s."""

    def test_retry_after_capped_at_10(self, monkeypatch):
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                err = urllib.error.HTTPError(
                    req.full_url, 403, "rate limited", {"Retry-After": "999"}, None
                )
                raise err
            return FakeResponse({"items": []}, headers={})

        sleep_calls = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        _make_request("https://api.github.com/test", "token")
        assert sleep_calls == [10]  # capped at 10


class TestParseNextLinkSecurity:
    """Test that _parse_next_link rejects non-GitHub URLs."""

    def test_rejects_non_github_url(self):
        headers = {
            "Link": '<https://evil.example.com/repos?page=2>; rel="next"'
        }
        assert _parse_next_link(headers) is None

    def test_case_insensitive_link_header(self):
        headers = {
            "link": '<https://api.github.com/search/repos?page=2>; rel="next"'
        }
        result = _parse_next_link(headers)
        assert result == "https://api.github.com/search/repos?page=2"


class TestRelativeTimeAllBuckets:
    """Tests for _relative_time covering all time buckets."""

    def _iso(self, delta):
        ts = datetime.now(timezone.utc) - delta
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_none_input(self):
        assert _relative_time(None) == ""

    def test_just_now(self):
        assert _relative_time(self._iso(timedelta(seconds=30))) == "just now"

    def test_minutes_ago(self):
        assert _relative_time(self._iso(timedelta(minutes=15))) == "15m ago"

    def test_hours_ago(self):
        assert _relative_time(self._iso(timedelta(hours=5))) == "5h ago"

    def test_weeks_ago(self):
        assert _relative_time(self._iso(timedelta(weeks=2))) == "2w ago"

    def test_months_ago(self):
        assert _relative_time(self._iso(timedelta(days=90))) == "3mo ago"

    def test_years_ago(self):
        assert _relative_time(self._iso(timedelta(days=400))) == "1y ago"


class TestGetAuthenticatedUser:
    """Tests for _get_authenticated_user."""

    def test_returns_login_on_success(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return FakeResponse({"login": "testuser"})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert _get_authenticated_user("fake-token") == "testuser"

    def test_returns_none_without_token(self):
        assert _get_authenticated_user(None) is None

    def test_returns_none_on_api_error(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert _get_authenticated_user("bad-token") is None


class TestDiscoverRunCmdMineOnly:
    """Tests for discover run_cmd --mine flag."""

    def test_mine_without_token_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: None
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {"mine": True})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "requires authentication" in captured.err

    def test_mine_filters_to_own_repos(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: "tok"
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda t: "me"
        )
        repos = [
            {"full_name": "me/repo1", "description": "Mine", "updated_at": "2025-01-01T00:00:00Z", "owner": {"login": "me"}},
            {"full_name": "other/repo2", "description": "Not mine", "updated_at": "2025-01-01T00:00:00Z", "owner": {"login": "other"}},
        ]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda t: repos
        )
        # Provide a terminal width to avoid layout issues
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((120, 40)))

        discover_run_cmd(None, [], {"mine": True})
        captured = capsys.readouterr()
        assert "me/repo1" in captured.out
        assert "other/repo2" not in captured.out

    def test_mine_no_repos_found(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: "tok"
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda t: "me"
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda t: []
        )
        discover_run_cmd(None, [], {"mine": True})
        captured = capsys.readouterr()
        assert "No rlsbl-tagged repositories found for your account" in captured.out

    def test_mine_auth_user_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: "tok"
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda t: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda t: [{"full_name": "a/b"}]
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {"mine": True})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "could not determine" in captured.err


class TestDiscoverRunCmdErrors:
    """Tests for discover run_cmd error handling."""

    def test_http_error_403(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos",
            lambda t: (_ for _ in ()).throw(
                urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "403" in captured.err
        assert "gh auth login" in captured.err

    def test_http_error_500(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos",
            lambda t: (_ for _ in ()).throw(
                urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "500" in captured.err

    def test_url_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos",
            lambda t: (_ for _ in ()).throw(
                urllib.error.URLError("Connection refused")
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "could not reach" in captured.err

    def test_generic_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos",
            lambda t: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "boom" in captured.err


class TestDiscoverRunCmdTableOutput:
    """Tests for the table rendering in discover run_cmd."""

    def test_renders_table_with_repos(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: "tok"
        )
        repos = [
            {"full_name": "user/project-a", "description": "A project", "updated_at": "2025-06-01T00:00:00Z"},
            {"full_name": "user/project-b", "description": None, "updated_at": "2025-01-01T00:00:00Z"},
        ]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda t: repos
        )
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((120, 40)))

        discover_run_cmd(None, [], {})
        captured = capsys.readouterr()
        assert "rlsbl ecosystem (2 projects)" in captured.out
        assert "user/project-a" in captured.out
        assert "user/project-b" in captured.out
        assert "A project" in captured.out

    def test_truncates_long_descriptions(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_github_token", lambda: "tok"
        )
        repos = [
            {"full_name": "user/repo", "description": "x" * 300, "updated_at": "2025-06-01T00:00:00Z"},
        ]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda t: repos
        )
        # Narrow terminal to force truncation
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((60, 40)))

        discover_run_cmd(None, [], {})
        # Should not crash; description should be truncated


# ============================================================================
# record_gif.py
# ============================================================================

from rlsbl.commands.record_gif import _get_bin_command, run_cmd as gif_run_cmd


class TestGetBinCommand:
    """Tests for _get_bin_command in record_gif."""

    def test_returns_none_when_no_targets_detected(self, monkeypatch):
        monkeypatch.setattr("rlsbl.commands.record_gif.detect_targets", lambda d: [])
        ctx = MagicMock()
        ctx.project_root = Path("/fake/project")
        assert _get_bin_command(ctx) is None

    def test_returns_bin_command_from_template_vars(self, monkeypatch):
        mock_entry = ("npm", "/fake/package.json")
        monkeypatch.setattr("rlsbl.commands.record_gif.detect_targets", lambda d: [mock_entry])

        mock_module = MagicMock()
        mock_module.template_vars.return_value = {"binCommand": "my-cli"}
        monkeypatch.setattr("rlsbl.commands.record_gif.TARGETS", {"npm": mock_module})

        ctx = MagicMock()
        ctx.project_root = Path("/fake/project")
        assert _get_bin_command(ctx) == "my-cli"

    def test_returns_none_when_bin_command_empty(self, monkeypatch):
        mock_entry = ("npm", "/fake/package.json")
        monkeypatch.setattr("rlsbl.commands.record_gif.detect_targets", lambda d: [mock_entry])

        mock_module = MagicMock()
        mock_module.template_vars.return_value = {"binCommand": ""}
        monkeypatch.setattr("rlsbl.commands.record_gif.TARGETS", {"npm": mock_module})

        ctx = MagicMock()
        ctx.project_root = Path("/fake/project")
        assert _get_bin_command(ctx) is None

    def test_returns_none_when_target_not_in_registry(self, monkeypatch):
        mock_entry = ("unknown_target", "/fake/path")
        monkeypatch.setattr("rlsbl.commands.record_gif.detect_targets", lambda d: [mock_entry])
        monkeypatch.setattr("rlsbl.commands.record_gif.TARGETS", {})

        ctx = MagicMock()
        ctx.project_root = Path("/fake/project")
        assert _get_bin_command(ctx) is None

    def test_returns_none_on_template_vars_exception(self, monkeypatch):
        mock_entry = ("npm", "/fake/package.json")
        monkeypatch.setattr("rlsbl.commands.record_gif.detect_targets", lambda d: [mock_entry])

        mock_module = MagicMock()
        mock_module.template_vars.side_effect = RuntimeError("oops")
        monkeypatch.setattr("rlsbl.commands.record_gif.TARGETS", {"npm": mock_module})

        ctx = MagicMock()
        ctx.project_root = Path("/fake/project")
        assert _get_bin_command(ctx) is None


class TestGifRunCmd:
    """Tests for record_gif run_cmd."""

    def test_exits_when_vhs_missing(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: None)
        ctx = MagicMock()
        ctx.project_root = Path("/fake")
        with pytest.raises(SystemExit) as exc_info:
            gif_run_cmd(None, [], {}, ctx)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "vhs is required" in captured.err

    def test_exits_when_no_bin_command(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: "/usr/bin/vhs")
        monkeypatch.setattr("rlsbl.commands.record_gif._get_bin_command", lambda ctx: None)
        ctx = MagicMock()
        ctx.project_root = Path("/fake")
        with pytest.raises(SystemExit) as exc_info:
            gif_run_cmd(None, [], {}, ctx)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "could not detect" in captured.err

    def test_successful_recording(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: "/usr/bin/vhs")
        monkeypatch.setattr("rlsbl.commands.record_gif._get_bin_command", lambda ctx: "my-tool")
        monkeypatch.setattr("subprocess.run", MagicMock())

        ctx = MagicMock()
        ctx.project_root = tmp_path

        gif_run_cmd(None, [], {}, ctx)
        captured = capsys.readouterr()
        assert "Recording demo" in captured.out
        assert "Done" in captured.out

    def test_vhs_subprocess_failure(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: "/usr/bin/vhs")
        monkeypatch.setattr("rlsbl.commands.record_gif._get_bin_command", lambda ctx: "my-tool")
        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=subprocess.CalledProcessError(1, "vhs")))

        ctx = MagicMock()
        ctx.project_root = tmp_path

        with pytest.raises(SystemExit) as exc_info:
            gif_run_cmd(None, [], {}, ctx)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "vhs recording failed" in captured.err

    def test_vhs_timeout(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: "/usr/bin/vhs")
        monkeypatch.setattr("rlsbl.commands.record_gif._get_bin_command", lambda ctx: "my-tool")
        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=subprocess.TimeoutExpired("vhs", 120)))

        ctx = MagicMock()
        ctx.project_root = tmp_path

        with pytest.raises(SystemExit) as exc_info:
            gif_run_cmd(None, [], {}, ctx)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "timed out" in captured.err

    def test_custom_flags_passed_to_tape(self, monkeypatch, tmp_path):
        monkeypatch.setattr("rlsbl.commands.record_gif.require_tool", lambda n, fatal=True: "/usr/bin/vhs")
        monkeypatch.setattr("rlsbl.commands.record_gif._get_bin_command", lambda ctx: "my-tool")

        run_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            run_calls.append(cmd)

        monkeypatch.setattr("subprocess.run", mock_subprocess_run)

        ctx = MagicMock()
        ctx.project_root = tmp_path

        gif_run_cmd(None, [], {"width": "800", "height": "400", "font-size": "18", "duration": "5"}, ctx)

        # Verify vhs was called
        assert len(run_calls) == 1
        assert run_calls[0][0] == "vhs"


# ============================================================================
# prs.py
# ============================================================================

from rlsbl.commands.prs import run_cmd as prs_run_cmd


class TestPrsRunCmd:
    """Tests for prs.py run_cmd."""

    def test_exits_0_when_gh_not_installed(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_installed", lambda: False)
        with pytest.raises(SystemExit) as exc_info:
            prs_run_cmd(None, [], {})
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "gh CLI not found" in captured.err

    def test_exits_0_when_gh_not_authenticated(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_installed", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_auth", lambda: False)
        with pytest.raises(SystemExit) as exc_info:
            prs_run_cmd(None, [], {})
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "not authenticated" in captured.err

    def test_prints_count_when_prs_exist(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_installed", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_auth", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.run_gh", lambda args, **kw: "3")
        monkeypatch.setattr("rlsbl.commands.prs.get_github_repo", lambda *a, **kw: None)
        monkeypatch.setattr("subprocess.run", MagicMock())

        with pytest.raises(SystemExit) as exc_info:
            prs_run_cmd(None, [], {})
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Open PRs: 3" in captured.out

    def test_no_output_when_zero_prs(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_installed", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_auth", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.run_gh", lambda args, **kw: "0")

        with pytest.raises(SystemExit) as exc_info:
            prs_run_cmd(None, [], {})
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Open PRs" not in captured.out

    def test_handles_exception_gracefully(self, monkeypatch, capsys):
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_installed", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.check_gh_auth", lambda: True)
        monkeypatch.setattr("rlsbl.commands.prs.run_gh", MagicMock(side_effect=RuntimeError("network down")))

        with pytest.raises(SystemExit) as exc_info:
            prs_run_cmd(None, [], {})
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "could not list PRs" in captured.err


# ============================================================================
# edit_release.py
# ============================================================================

from rlsbl.commands.edit_release import run_cmd as edit_release_run_cmd


class TestEditReleaseAdditionalCoverage:
    """Additional tests for edit_release.py uncovered paths."""

    def test_gh_not_installed_exits(self, capsys):
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                edit_release_run_cmd([], {}, project_root=".")
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "gh CLI is not installed" in captured.err

    def test_gh_not_authenticated_exits(self, capsys):
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                edit_release_run_cmd([], {}, project_root=".")
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "not authenticated" in captured.err

    def test_no_targets_detected(self, capsys):
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.find_workspace_root", return_value=None), \
             patch("rlsbl.commands.edit_release.resolve_member_context", return_value=MagicMock(targets=[])):
            with pytest.raises(SystemExit) as exc_info:
                edit_release_run_cmd([], {}, project_root=".")
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "no package.json" in captured.err

    def test_changelog_not_found(self, capsys, tmp_path):
        mock_target = MagicMock()
        mock_target.read_version.return_value = "1.0.0"
        mock_target.tag_format.side_effect = lambda v: f"v{v}"
        mock_entry = MagicMock()
        mock_entry.name = "npm"
        mock_entry.path = str(tmp_path / "package.json")

        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.find_workspace_root", return_value=None), \
             patch("rlsbl.commands.edit_release.resolve_member_context", return_value=MagicMock(targets=[mock_entry])), \
             patch("rlsbl.commands.edit_release.TARGETS", {"npm": mock_target}):
            with pytest.raises(SystemExit) as exc_info:
                edit_release_run_cmd([], {}, project_root=str(tmp_path))
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "CHANGELOG.md not found" in captured.err

    def test_non_releasable_project_exits(self, capsys, tmp_path):
        """Non-releasable monorepo project exits with error."""
        mock_target = MagicMock()
        mock_entry = MagicMock()
        mock_entry.name = "npm"

        mock_project = MagicMock()
        mock_project.__getitem__ = lambda self, key: {"name": "test", "path": "test"}[key]
        mock_project.is_releasable = False

        # is_explicit_mode is called inside the monorepo block before the
        # is_non_releasable check; mock it to avoid filesystem access.
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.find_workspace_root", return_value=str(tmp_path)), \
             patch("rlsbl.commands.edit_release.resolve_project", return_value=mock_project), \
             patch("rlsbl.commands.edit_release.resolve_member_context", return_value=MagicMock(targets=[mock_entry])), \
             patch("rlsbl.commands.edit_release.TARGETS", {"npm": mock_target}):
            with pytest.raises(SystemExit) as exc_info:
                edit_release_run_cmd([], {}, project_root=".")
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "non-releasable" in captured.err


# ============================================================================
# claim_name.py
# ============================================================================

from rlsbl.commands.claim_name import run_cmd as claim_run_cmd


class TestClaimNameAdditionalCoverage:
    """Additional tests for claim_name.py uncovered paths."""

    def test_wrong_arg_count_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", [], {"yes": False})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Expected exactly one" in captured.err

    def test_two_args_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", ["a", "b"], {"yes": False})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Expected exactly one" in captured.err

    def test_unsupported_target_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("go", ["my-pkg"], {"yes": False})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Unsupported target" in captured.err

    @patch("rlsbl.commands.check._check_single_name")
    def test_ambiguous_status_without_yes_exits(self, mock_check, capsys):
        mock_check.return_value = {
            "name": "pkg", "registry": "npm", "status": "unknown",
            "variants": None, "reason": None,
        }
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", ["pkg"], {"yes": False})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Ambiguous status" in captured.err

    @patch("rlsbl.commands.claim_name.subprocess.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_ambiguous_status_with_yes_proceeds(self, mock_check, mock_run, capsys, tmp_path):
        mock_check.return_value = {
            "name": "pkg", "registry": "npm", "status": "unknown",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)

        with patch("rlsbl.commands.claim_name.tempfile.mkdtemp", return_value=str(tmp_path)), \
             patch("rlsbl.commands.claim_name.shutil.rmtree"), \
             patch.dict(os.environ, {"NPM_TOKEN": "tok"}):
            claim_run_cmd("npm", ["pkg"], {"yes": True})

        captured = capsys.readouterr()
        assert "--yes passed" in captured.out


# ============================================================================
# deploy_cmd.py
# ============================================================================

from rlsbl.commands.deploy_cmd import run_cmd as deploy_run_cmd, _print_dry_run
from rlsbl.deploy import DeployResult
from rlsbl.context import ProjectContext


class TestDeployCmdAdditionalCoverage:
    """Additional tests for deploy_cmd.py uncovered paths."""

    def test_deploy_config_validation_errors(self, capsys):
        config = {
            "deploy": [{"name": "prod"}],  # missing required fields
        }
        with pytest.raises(SystemExit) as exc_info:
            deploy_run_cmd(
                None, [], {},
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config=config),
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "validation errors" in captured.err

    def test_deploy_failure_with_rollback(self, monkeypatch, capsys):
        targets = [{
            "name": "prod",
            "host": "10.0.0.1",
            "steps": ["systemctl restart app"],
            "only_on": ["main"],
        }]

        def mock_deploy(target_config, branch):
            return DeployResult("prod", False, "Health check failed", rolled_back=True)

        monkeypatch.setattr("rlsbl.commands.deploy_cmd.deploy_target", mock_deploy)
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda: "main")

        with pytest.raises(SystemExit) as exc_info:
            deploy_run_cmd(
                None, [], {},
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"deploy": targets}),
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "rolled back" in captured.err

    def test_dry_run_tcp_health(self, capsys):
        target_config = {
            "name": "prod",
            "host": "10.0.0.1",
            "steps": ["restart"],
            "only_on": ["main"],
            "health": {"type": "tcp", "port": 8080},
        }
        _print_dry_run(target_config, "main")
        captured = capsys.readouterr()
        assert "tcp" in captured.out
        assert "8080" in captured.out

    def test_dry_run_script_health(self, capsys):
        target_config = {
            "name": "prod",
            "host": "10.0.0.1",
            "steps": ["restart"],
            "only_on": ["main"],
            "health": {"type": "script", "command": "/usr/local/bin/check.sh"},
        }
        _print_dry_run(target_config, "main")
        captured = capsys.readouterr()
        assert "script" in captured.out
        assert "check.sh" in captured.out

    def test_dry_run_unknown_health_type(self, capsys):
        target_config = {
            "name": "prod",
            "host": "10.0.0.1",
            "steps": ["restart"],
            "only_on": ["main"],
            "health": {"type": "custom"},
        }
        _print_dry_run(target_config, "main")
        captured = capsys.readouterr()
        assert "custom" in captured.out


# ============================================================================
# changelog/files.py
# ============================================================================

from rlsbl.changelog.files import (
    append_entry_to_version,
    unfinalize_version,
    writable_jsonl,
    remap_jsonl_hashes,
    RemapResult,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry


class TestAppendEntryToVersion:
    """Tests for append_entry_to_version."""

    def test_appends_to_versioned_file(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        versioned = changes / "1.0.0.jsonl"
        versioned.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        entry = ChangelogEntry(
            commits=["def"],
            user_facing=True,
            description="New feature",
            type="feature",
        )
        append_entry_to_version(str(changes), "1.0.0", entry)

        entries = parse_jsonl(str(versioned))
        assert len(entries) == 2
        assert entries[1].commits == ["def"]
        assert entries[1].description == "New feature"

    def test_creates_file_if_missing(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        append_entry_to_version(str(changes), "2.0.0", entry)

        versioned = changes / "2.0.0.jsonl"
        assert versioned.exists()
        entries = parse_jsonl(str(versioned))
        assert len(entries) == 1


class TestUnfinalizeVersion:
    """Tests for unfinalize_version."""

    def test_unfinalizes_versioned_file(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        # Create a finalized versioned file
        versioned = changes / "1.0.0.jsonl"
        versioned.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )
        os.chmod(str(versioned), 0o444)

        # Create the per-version md file
        versioned_md = changes / "1.0.0.md"
        versioned_md.write_text("## 1.0.0\n\n- stuff\n")

        # Create existing unreleased.jsonl (will be overwritten)
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text("")

        changed = unfinalize_version(str(changes), "1.0.0")

        assert unreleased.exists()
        entries = parse_jsonl(str(unreleased))
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

        assert not versioned.exists()
        assert not versioned_md.exists()

        assert str(unreleased) in changed
        assert str(versioned_md) in changed

    def test_returns_empty_when_versioned_missing(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        result = unfinalize_version(str(changes), "9.9.9")
        assert result == []

    def test_unfinalizes_without_md_file(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        versioned = changes / "1.0.0.jsonl"
        versioned.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )
        os.chmod(str(versioned), 0o444)

        changed = unfinalize_version(str(changes), "1.0.0")

        unreleased = changes / "unreleased.jsonl"
        assert unreleased.exists()
        # Only unreleased in changed list (no md file to delete)
        assert len(changed) == 1
        assert str(unreleased) in changed


class TestWritableJsonl:
    """Tests for writable_jsonl context manager."""

    def test_makes_read_only_writable_then_restores(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("content\n")
        os.chmod(str(f), 0o444)

        assert not (os.stat(str(f)).st_mode & stat.S_IWUSR)

        with writable_jsonl(str(f)) as path:
            # File should be writable during context
            assert os.stat(path).st_mode & stat.S_IWUSR
            with open(path, "a") as fp:
                fp.write("more\n")

        # File should be read-only again after context
        assert not (os.stat(str(f)).st_mode & stat.S_IWUSR)

    def test_no_op_for_already_writable(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("content\n")
        original_mode = os.stat(str(f)).st_mode

        with writable_jsonl(str(f)) as path:
            pass

        # Mode should not have changed
        assert os.stat(str(f)).st_mode == original_mode

    def test_restores_on_exception(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("content\n")
        os.chmod(str(f), 0o444)

        with pytest.raises(ValueError):
            with writable_jsonl(str(f)):
                raise ValueError("oops")

        # Should still be restored to read-only
        assert not (os.stat(str(f)).st_mode & stat.S_IWUSR)


class TestRemapJsonlHashes:
    """Tests for remap_jsonl_hashes."""

    def test_remaps_hashes_in_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": ["old1"], "user_facing": False}) + "\n"
            + json.dumps({"commits": ["old2", "keep"], "user_facing": True, "description": "X", "type": "fix"}) + "\n"
        )

        sha_map = {"old1": "new1", "old2": "new2"}
        results = remap_jsonl_hashes(str(changes), sha_map).results

        assert len(results) == 1
        assert results[0].entries_modified == 2
        assert results[0].hashes_remapped == 2

        entries = parse_jsonl(str(unreleased))
        assert entries[0].commits == ["new1"]
        assert entries[1].commits == ["new2", "keep"]

    def test_remaps_in_versioned_file(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        versioned = changes / "1.0.0.jsonl"
        versioned.write_text(
            json.dumps({"commits": ["aaa"], "user_facing": False}) + "\n"
        )
        os.chmod(str(versioned), 0o444)

        sha_map = {"aaa": "bbb"}
        results = remap_jsonl_hashes(str(changes), sha_map).results

        assert len(results) == 1
        entries = parse_jsonl(str(versioned))
        assert entries[0].commits == ["bbb"]

        # File should be back to read-only
        assert not (os.stat(str(versioned)).st_mode & stat.S_IWUSR)

    def test_no_match_returns_empty(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        sha_map = {"xxx": "yyy"}
        results = remap_jsonl_hashes(str(changes), sha_map).results
        assert results == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        results = remap_jsonl_hashes(str(tmp_path / "nope"), {"a": "b"}).results
        assert results == []


# ============================================================================
# changelog/resolve.py
# ============================================================================

from rlsbl.changelog.resolve import resolve_hash, resolve_hashes


class TestResolveHash:
    """Tests for resolve_hash."""

    def test_returns_none_for_invalid_hash(self, mock_git_repo):
        result = resolve_hash("0000000000000000000000000000000000000000")
        assert result is None

    def test_returns_full_sha_for_valid_hash(self, mock_git_repo):
        sha = git_head(mock_git_repo)
        result = resolve_hash(sha[:8])
        assert result == sha

    def test_returns_none_on_non_40_char_output(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "short\n"
        monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)
        assert resolve_hash("abc") is None

    def test_returns_none_on_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.TimeoutExpired("git", 10)),
        )
        assert resolve_hash("abc") is None

    def test_returns_none_on_file_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=FileNotFoundError("no git")),
        )
        assert resolve_hash("abc") is None


class TestResolveHashes:
    """Tests for resolve_hashes batch function."""

    def test_deduplicates_lookups(self, monkeypatch):
        call_count = 0

        def mock_resolve(h, *, cwd=None):
            nonlocal call_count
            call_count += 1
            return "a" * 40

        monkeypatch.setattr("rlsbl.changelog.resolve.resolve_hash", mock_resolve)

        result = resolve_hashes(["abc", "abc", "def", "abc"])
        assert call_count == 2  # only abc and def resolved
        assert len(result) == 2


# ============================================================================
# release_retry.py
# ============================================================================

from rlsbl.commands.release_retry import (
    _find_dispatch_workflows,
    _cleanup_retry_file,
    _scaffold_retry_file,
    run_cmd as retry_run_cmd,
)
from rlsbl.release_file import RetryConfig


class TestFindDispatchWorkflowsOSError:
    """Tests for _find_dispatch_workflows with I/O errors."""

    def test_handles_oserror_reading_file(self, tmp_path, monkeypatch):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on:\n  workflow_dispatch:\n")

        monkeypatch.chdir(tmp_path)

        original_open = open

        def error_open(path, *args, **kwargs):
            if str(path).endswith("ci.yml"):
                raise OSError("permission denied")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=error_open):
            result = _find_dispatch_workflows()

        assert result == []


class TestCleanupRetryFile:
    """Tests for _cleanup_retry_file."""

    def test_successful_cleanup(self, monkeypatch):
        log_msgs = []
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(return_value=MagicMock(returncode=0)),
        )
        _cleanup_retry_file("/fake/retry.toml", lambda msg: log_msgs.append(msg))
        assert any("Cleaned up" in m for m in log_msgs)

    def test_cleanup_failure_is_non_fatal(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "saferm")),
        )
        log_msgs = []
        _cleanup_retry_file("/fake/retry.toml", lambda msg: log_msgs.append(msg))
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_cleanup_file_not_found_non_fatal(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=FileNotFoundError("no saferm")),
        )
        _cleanup_retry_file("/fake/retry.toml", lambda msg: None)
        captured = capsys.readouterr()
        assert "Warning" in captured.err


class TestReleaseRetryQuietMode:
    """Tests for release_retry quiet mode."""

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run_gh")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_quiet_suppresses_output(self, _gh_inst, _gh_auth, _ws_root,
                                      mock_targets_dict, mock_detect,
                                      _exists, mock_run, mock_run_gh,
                                      mock_cleanup, capsys):
        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        def run_effect(cmd, args=None, **kwargs):
            if cmd == "git" and args and args[:2] == ["rev-list", "-1"]:
                return "a" * 40
            return ""

        mock_run.side_effect = run_effect

        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return ""
            if args[:2] == ["workflow", "run"]:
                return ""
            if args[:2] == ["run", "list"]:
                return "[]"
            return ""

        mock_run_gh.side_effect = run_gh_effect
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0")

        with patch("rlsbl.commands.release_retry.time.sleep"):
            retry_run_cmd(config, {"yes": True, "quiet": True}, project_root=".")

        captured = capsys.readouterr()
        # Quiet mode should suppress "Dispatching workflows..." etc.
        assert "Dispatching" not in captured.out


class TestReleaseRetryConfirmationEOFError:
    """Tests for confirmation prompt edge cases."""

    @patch("rlsbl.commands.release_retry.run_gh", return_value="")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_eof_during_confirmation(self, _gh_inst, _gh_auth, _ws_root,
                                      mock_targets_dict, mock_detect,
                                      _exists, mock_run, _run_gh, capsys):
        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        mock_run.return_value = "a" * 40  # git rev-list
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0")

        with patch("builtins.input", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                retry_run_cmd(config, {}, project_root=".")
        assert exc_info.value.code == 1

    @patch("rlsbl.commands.release_retry.run_gh", return_value="")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_keyboard_interrupt_during_confirmation(self, _gh_inst, _gh_auth, _ws_root,
                                                     mock_targets_dict, mock_detect,
                                                     _exists, mock_run, _run_gh, capsys):
        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        mock_run.return_value = "a" * 40  # git rev-list
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0")

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                retry_run_cmd(config, {}, project_root=".")
        assert exc_info.value.code == 1


class TestReleaseRetryNoTargets:
    """Test release retry when no targets are detected."""

    @patch("rlsbl.commands.release_retry.resolve_member_context", return_value=MagicMock(targets=[]))
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_no_targets_exits(self, _gh_inst, _gh_auth, _ws_root, _detect, capsys):
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0")
        with pytest.raises(SystemExit) as exc_info:
            retry_run_cmd(config, {"yes": True}, project_root=".")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "no package.json" in captured.err


class TestReleaseRetryDispatchWarning:
    """Test that dispatch failures produce warnings, not hard errors."""

    @patch("rlsbl.commands.release_retry._cleanup_retry_file")
    @patch("rlsbl.commands.release_retry.run_gh")
    @patch("rlsbl.commands.release_retry.run")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.release_retry.resolve_member_context")
    @patch("rlsbl.commands.release_retry.TARGETS")
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_dispatch_failure_warns(self, _gh_inst, _gh_auth, _ws_root,
                                     mock_targets_dict, mock_detect,
                                     _exists, mock_run, mock_run_gh,
                                     mock_cleanup, capsys):
        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        mock_run.return_value = "a" * 40  # git rev-list

        def run_gh_effect(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return ""
            if args[:2] == ["workflow", "run"]:
                raise RuntimeError("dispatch failed")
            return ""

        mock_run_gh.side_effect = run_gh_effect
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0")

        retry_run_cmd(config, {"yes": True}, project_root=".")
        captured = capsys.readouterr()
        assert "failed to dispatch" in captured.err


# ============================================================================
# mirror_cmd.py (additional via mock-heavy approach)
# ============================================================================

import rlsbl.commands.monorepo.mirror_cmd
from rlsbl.commands.monorepo.mirror_cmd import _cmd_mirror


class TestMirrorCmdSuccessPath:
    """Test the success path of mirror_cmd via heavy mocking."""

    def test_successful_subtree_split_and_push(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        """Mock the full subtree split + push + clone + scaffold path."""
        from rlsbl.workspace import WORKSPACE_DIR, save_workspace

        # Create a bare repo to serve as the subtree remote
        bare_repo = tmp_path / "bare-remote.git"
        bare_repo.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q"],
            cwd=str(bare_repo),
            check=True,
        )

        # Set up workspace with project
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(json.dumps({"name": "mylib", "version": "0.1.0"}))

        proj = {"path": "mylib", "name": "mylib", "subtree_remote": str(bare_repo)}
        ws_dir = mock_git_repo / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        save_workspace(str(mock_git_repo), [proj])

        run_git(mock_git_repo, "add", "mylib")
        run_git(mock_git_repo, "add", WORKSPACE_DIR)
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Mock the subtree split and push (they need real git subtree support)
        run_calls = []
        original_run = subprocess.run

        def mock_run_fn(cmd, **kwargs):
            if isinstance(cmd, list) and "subtree" in cmd:
                # Create the temp branch manually to simulate subtree split
                subprocess.run(
                    ["git", "branch", "_rlsbl-mirror-tmp", "HEAD"],
                    cwd=str(mock_git_repo),
                    check=True,
                    capture_output=True,
                )
                return MagicMock(returncode=0)
            run_calls.append(cmd)
            return original_run(cmd, **kwargs)

        # We need to mock validate_subtree_remote_ssh_host since bare_repo is a local path
        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )

        # Mock the entire clone+scaffold portion
        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.run",
            lambda cmd, args, cwd=None: "",
        )

        # Mock subprocess.run for scaffold and other commands
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=0, stdout="", stderr=""),
        )

        # Mock tempfile.mkdtemp
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / ".rlsbl").mkdir()
        monkeypatch.setattr(
            "tempfile.mkdtemp",
            lambda **kw: str(clone_dir),
        )

        _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)

        captured = capsys.readouterr()
        assert "Splitting subtree" in captured.out


class TestMirrorCmdSubtreeSplitFailure:
    """Test mirror_cmd when subtree split fails."""

    def test_subtree_split_failure_exits(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        from rlsbl.workspace import WORKSPACE_DIR, save_workspace

        # Create a bare repo to serve as the subtree remote
        bare_repo = tmp_path / "bare-remote.git"
        bare_repo.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q"],
            cwd=str(bare_repo),
            check=True,
        )

        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(json.dumps({"name": "mylib", "version": "0.1.0"}))

        proj = {"path": "mylib", "name": "mylib", "subtree_remote": str(bare_repo)}
        ws_dir = mock_git_repo / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        save_workspace(str(mock_git_repo), [proj])

        run_git(mock_git_repo, "add", "mylib")
        run_git(mock_git_repo, "add", WORKSPACE_DIR)
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )

        # Make the run function raise CalledProcessError for subtree split
        original_run = rlsbl.commands.monorepo.mirror_cmd.run

        def mock_run(cmd, args, cwd=None):
            if cmd == "git" and "subtree" in args:
                raise subprocess.CalledProcessError(
                    1, ["git", "subtree", "split"],
                    stderr="fatal: not a valid object"
                )
            return original_run(cmd, args, cwd=cwd)

        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.run", mock_run,
        )

        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "subtree split failed" in captured.err


class TestMirrorCmdPushFailure:
    """Test mirror_cmd when push to mirror fails."""

    def test_push_failure_cleans_up_branch(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        from rlsbl.workspace import WORKSPACE_DIR, save_workspace

        bare_repo = tmp_path / "bare-remote.git"
        bare_repo.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q"],
            cwd=str(bare_repo),
            check=True,
        )

        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(json.dumps({"name": "mylib", "version": "0.1.0"}))

        proj = {"path": "mylib", "name": "mylib", "subtree_remote": str(bare_repo)}
        ws_dir = mock_git_repo / WORKSPACE_DIR
        ws_dir.mkdir(exist_ok=True)
        save_workspace(str(mock_git_repo), [proj])

        run_git(mock_git_repo, "add", "mylib")
        run_git(mock_git_repo, "add", WORKSPACE_DIR)
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.validate_subtree_remote_ssh_host",
            lambda remote, root: None,
        )

        call_count = {"n": 0}

        def mock_run(cmd, args, cwd=None):
            call_count["n"] += 1
            if cmd == "git" and args[0] == "subtree":
                # Create the temp branch to simulate subtree split
                subprocess.run(
                    ["git", "branch", "_rlsbl-mirror-tmp", "HEAD"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                )
                return ""
            if cmd == "git" and args[0] == "push":
                raise subprocess.CalledProcessError(
                    1, ["git", "push"],
                    stderr="remote: Permission denied"
                )
            if cmd == "git" and args[0] == "branch" and args[1] == "-D":
                subprocess.run(
                    ["git", "branch", "-D", args[2]],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                )
                return ""
            return ""

        monkeypatch.setattr(
            "rlsbl.commands.monorepo.mirror_cmd.run", mock_run,
        )

        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "push to mirror failed" in captured.err


# ============================================================================
# edit_release.py -- monorepo tag format path
# ============================================================================


class TestEditReleaseMonorepoTagFormat:
    """Tests for edit_release monorepo tag format path."""

    @patch("rlsbl.commands.edit_release.run_gh")
    @patch("rlsbl.commands.edit_release.extract_changelog_entry", return_value="- Fixed bug")
    @patch("os.path.exists", return_value=True)
    @patch("rlsbl.commands.edit_release.resolve_member_context")
    @patch("rlsbl.commands.edit_release.TARGETS")
    @patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True)
    def test_monorepo_tag_format_used(self, _gh_inst, _gh_auth, mock_targets_dict,
                                       mock_detect, _exists, mock_extract, mock_run_gh):
        """In monorepo context without explicit releasable, uses monorepo_tag_format."""
        target = MagicMock()
        target.read_version.return_value = "1.0.0"
        target.tag_format.side_effect = lambda v: f"v{v}"
        target.monorepo_tag_format.side_effect = lambda name, v, path=None: f"{name}@v{v}"

        entry = MagicMock()
        entry.name = "npm"
        entry.path = "."
        mock_detect.return_value = MagicMock(targets=[entry])
        mock_targets_dict.__getitem__ = lambda self, key: target

        mock_project = MagicMock()
        mock_project.__getitem__ = lambda self, key: {"name": "my-pkg", "path": "packages/my-pkg"}[key]
        mock_project.is_releasable = True

        with patch("rlsbl.commands.edit_release.find_workspace_root", return_value="/tmp/test"), \
             patch("rlsbl.commands.edit_release.resolve_project", return_value=mock_project), \
             patch("rlsbl.workspace.is_explicit_mode", return_value=False), \
             patch("builtins.open", mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            edit_release_run_cmd(["1.0.0"], {}, project_root=".")

        # Should use monorepo tag format
        target.monorepo_tag_format.assert_called_once_with(
            "my-pkg", "1.0.0", path="packages/my-pkg"
        )
        assert any(c[0] == (["release", "view", "my-pkg@v1.0.0"],) for c in mock_run_gh.call_args_list)
