"""Targeted tests to increase code coverage for low-coverage modules.

Covers: discover.py, mirror_cmd.py, prs.py, migrate.py,
edit_release.py, claim_name.py, deploy_cmd.py, changelog/files.py,
changelog/resolve.py, release_retry.py.
"""

import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

from conftest import git_head, run_git, make_workspace


# ============================================================================
# discover.py
# ============================================================================

from rlsbl.commands.discover import (
    _gh_api,
    _relative_time,
    _get_authenticated_user,
    _fetch_all_repos,
    run_cmd as discover_run_cmd,
)


def _tsv(*rows):
    """Render repo rows the way gh's --jq @tsv projection would."""
    return "".join("\t".join(row) + "\n" for row in rows)


class TestGhApi:
    """_gh_api pins --method GET, which is what makes it an observe."""

    def test_pins_method_get(self, monkeypatch):
        seen = {}

        def fake_gh(args, **kwargs):
            seen["args"] = list(args)
            return SimpleNamespace(stdout="out", stderr="", returncode=0)

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        assert _gh_api(["user"], timeout=1) == "out"
        assert seen["args"][:4] == ["api", "--method", "GET", "user"]

    def test_returns_none_when_gh_fails(self, monkeypatch):
        def fake_gh(args, **kwargs):
            raise subprocess.CalledProcessError(1, ["gh"])

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        assert _gh_api(["user"], timeout=1) is None

    def test_returns_none_when_gh_is_missing(self, monkeypatch):
        def fake_gh(args, **kwargs):
            raise FileNotFoundError("gh")

        monkeypatch.setattr("rlsbl.commands.discover.effects.gh", fake_gh)
        assert _gh_api(["user"], timeout=1) is None


class TestFetchAllRepos:
    """_fetch_all_repos parses gh's paginated TSV projection."""

    def test_parses_rows(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api",
            lambda args, timeout: _tsv(
                ("a/one", "first", "2025-01-01T00:00:00Z", "a"),
                ("b/two", "", "2025-01-02T00:00:00Z", "b"),
            ),
        )
        assert _fetch_all_repos() == [
            ("a/one", "first", "2025-01-01T00:00:00Z", "a"),
            ("b/two", "", "2025-01-02T00:00:00Z", "b"),
        ]

    def test_asks_gh_to_paginate(self, monkeypatch):
        seen = {}

        def fake(args, timeout):
            seen["args"] = list(args)
            return ""

        monkeypatch.setattr("rlsbl.commands.discover._gh_api", fake)
        _fetch_all_repos()
        assert "--paginate" in seen["args"]

    def test_caps_at_max_results(self, monkeypatch):
        from rlsbl.commands.discover import MAX_RESULTS

        rows = _tsv(*[
            (f"u/r{i}", "", "2025-01-01T00:00:00Z", "u")
            for i in range(MAX_RESULTS + 25)
        ])
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: rows,
        )
        assert len(_fetch_all_repos()) == MAX_RESULTS

    def test_skips_malformed_rows(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api",
            lambda args, timeout: "a/one\tdesc\n" + _tsv(
                ("b/two", "", "2025-01-02T00:00:00Z", "b"),
            ),
        )
        assert _fetch_all_repos() == [
            ("b/two", "", "2025-01-02T00:00:00Z", "b"),
        ]

    def test_returns_none_when_gh_cannot_answer(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: None,
        )
        assert _fetch_all_repos() is None


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
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: "testuser\n",
        )
        assert _get_authenticated_user() == "testuser"

    def test_returns_none_when_gh_cannot_answer(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: None,
        )
        assert _get_authenticated_user() is None

    def test_returns_none_on_empty_answer(self, monkeypatch):
        monkeypatch.setattr(
            "rlsbl.commands.discover._gh_api", lambda args, timeout: "\n",
        )
        assert _get_authenticated_user() is None


class TestDiscoverRunCmdMineOnly:
    """Tests for discover run_cmd --mine flag."""

    def test_mine_filters_to_own_repos(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda: "me"
        )
        repos = [
            ("me/repo1", "Mine", "2025-01-01T00:00:00Z", "me"),
            ("other/repo2", "Not mine", "2025-01-01T00:00:00Z", "other"),
        ]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: repos
        )
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((120, 40)))

        discover_run_cmd(None, [], {"mine": True})
        captured = capsys.readouterr()
        assert "me/repo1" in captured.out
        assert "other/repo2" not in captured.out

    def test_mine_no_repos_found(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda: "me"
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: []
        )
        discover_run_cmd(None, [], {"mine": True})
        captured = capsys.readouterr()
        assert "No rlsbl-tagged repositories found for your account" in captured.out

    def test_mine_auth_user_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._get_authenticated_user", lambda: None
        )
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos",
            lambda: [("a/b", "", "2025-01-01T00:00:00Z", "a")],
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {"mine": True})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "could not determine" in captured.err


class TestDiscoverRunCmdErrors:
    """Tests for discover run_cmd error handling."""

    def test_exits_when_gh_cannot_answer(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: None
        )
        with pytest.raises(SystemExit) as exc_info:
            discover_run_cmd(None, [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "gh auth login" in captured.err


class TestDiscoverRunCmdTableOutput:
    """Tests for the table rendering in discover run_cmd."""

    def test_renders_table_with_repos(self, monkeypatch, capsys):
        repos = [
            ("user/project-a", "A project", "2025-06-01T00:00:00Z", "user"),
            ("user/project-b", "", "2025-01-01T00:00:00Z", "user"),
        ]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: repos
        )
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((120, 40)))

        discover_run_cmd(None, [], {})
        captured = capsys.readouterr()
        assert "rlsbl ecosystem (2 projects)" in captured.out
        assert "user/project-a" in captured.out
        assert "user/project-b" in captured.out
        assert "A project" in captured.out

    def test_truncates_long_descriptions(self, monkeypatch, capsys):
        repos = [("user/repo", "x" * 300, "2025-06-01T00:00:00Z", "user")]
        monkeypatch.setattr(
            "rlsbl.commands.discover._fetch_all_repos", lambda: repos
        )
        monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((80, 24)))

        discover_run_cmd(None, [], {})
        captured = capsys.readouterr()
        assert "…" in captured.out
        assert "x" * 300 not in captured.out


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

        # The monorepo block resolves the releasable before the
        # is_non_releasable check; the workspace loaders are mocked below to
        # avoid filesystem access.
        with patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True), \
             patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True), \
             patch("rlsbl.commands.edit_release.find_workspace_root", return_value=str(tmp_path)), \
             patch("rlsbl.commands.edit_release.resolve_project", return_value=mock_project), \
             patch("rlsbl.workspace.load_workspace", return_value=[]), \
             patch("rlsbl.workspace.load_releasables", return_value=[]), \
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
            claim_run_cmd("npm", [], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Expected exactly one" in captured.err

    def test_two_args_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", ["a", "b"], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Expected exactly one" in captured.err

    def test_unsupported_target_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("go", ["my-pkg"], {})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Unsupported target" in captured.err

    @patch("rlsbl.commands.check._check_single_name")
    def test_ambiguous_status_without_force_publish_exits(self, mock_check, capsys):
        mock_check.return_value = {
            "name": "pkg", "registry": "npm", "status": "unknown",
            "variants": None, "reason": None,
        }
        with pytest.raises(SystemExit) as exc_info:
            claim_run_cmd("npm", ["pkg"], {"force-publish": False})
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Ambiguous status" in captured.err

    @patch("rlsbl.effects.run")
    @patch("rlsbl.commands.check._check_single_name")
    def test_ambiguous_status_with_force_publish_proceeds(self, mock_check, mock_run, capsys, tmp_path):
        mock_check.return_value = {
            "name": "pkg", "registry": "npm", "status": "unknown",
            "variants": None, "reason": None,
        }
        mock_run.return_value = MagicMock(returncode=0)

        with patch("rlsbl._effects_direct.mkdtemp", return_value=str(tmp_path)), \
             patch("rlsbl.effects.rmtree"), \
             patch.dict(os.environ, {"NPM_TOKEN": "tok"}):
            claim_run_cmd("npm", ["pkg"], {"force-publish": True})

        captured = capsys.readouterr()
        assert "--force-publish passed" in captured.out


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
        monkeypatch.setattr("rlsbl.commands.deploy_cmd.get_current_branch", lambda **k: "main")

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
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl


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

        with writable_jsonl(str(f)):
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
            "rlsbl.effects.run",
            MagicMock(return_value=MagicMock(returncode=0)),
        )
        _cleanup_retry_file("/fake/retry.toml", lambda msg: log_msgs.append(msg))
        assert any("Cleaned up" in m for m in log_msgs)

    def test_cleanup_failure_is_non_fatal(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.effects.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "saferm")),
        )
        log_msgs = []
        _cleanup_retry_file("/fake/retry.toml", lambda msg: log_msgs.append(msg))
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_cleanup_file_not_found_non_fatal(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "rlsbl.effects.run",
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
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0", tag="v1.0.0")

        with patch("rlsbl.commands.release_retry.time.sleep"):
            retry_run_cmd(config, {"quiet": True}, project_root=".")

        captured = capsys.readouterr()
        # Quiet mode should suppress "Dispatching workflows..." etc.
        assert "Dispatching" not in captured.out


class TestReleaseRetryNoTargets:
    """Test release retry when no targets are detected."""

    @patch("rlsbl.commands.release_retry.resolve_member_context", return_value=MagicMock(targets=[]))
    @patch("rlsbl.commands.release_retry.find_workspace_root", return_value=None)
    @patch("rlsbl.commands.release_retry.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release_retry.check_gh_installed", return_value=True)
    def test_no_targets_exits(self, _gh_inst, _gh_auth, _ws_root, _detect, capsys):
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0", tag="v1.0.0")
        with pytest.raises(SystemExit) as exc_info:
            retry_run_cmd(config, {}, project_root=".")
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
        config = RetryConfig(version="1.0.0", dispatch=["ci.yml"], ref="v1.0.0", tag="v1.0.0")

        retry_run_cmd(config, {}, project_root=".")
        captured = capsys.readouterr()
        assert "failed to dispatch" in captured.err


# ============================================================================
# mirror_cmd.py (additional via mock-heavy approach)
# ============================================================================

from rlsbl.commands.monorepo import mirror_cmd as mirror_mod
from rlsbl.commands.monorepo.mirror_cmd import _cmd_mirror, MirrorError, MirrorPlan


def _mirror_workspace(mock_git_repo, tmp_path):
    """Create a workspace with a single ``mylib`` project pointing at a bare
    subtree remote, and return the remote path. Shared setup for the
    error-propagation tests below.
    """
    from rlsbl.workspace import WORKSPACE_DIR

    bare_repo = tmp_path / "bare-remote.git"
    bare_repo.mkdir()
    subprocess.run(["git", "init", "--bare", "-q"], cwd=str(bare_repo), check=True)

    proj_dir = mock_git_repo / "mylib"
    proj_dir.mkdir()
    (proj_dir / "package.json").write_text(
        json.dumps({"name": "mylib", "version": "0.1.0"})
    )

    proj = {"path": "mylib", "name": "mylib", "releasable": "mylib"}
    (mock_git_repo / WORKSPACE_DIR).mkdir(exist_ok=True)
    make_workspace(
        str(mock_git_repo), [proj],
        releasables=[{"name": "mylib", "subtree_remote": str(bare_repo)}],
    )

    run_git(mock_git_repo, "add", "mylib")
    run_git(mock_git_repo, "add", WORKSPACE_DIR)
    run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")
    return str(bare_repo)


# The reconciler rewrite (observe-then-converge) is exercised end-to-end
# against real bare repos in tests/test_mirror_cmd.py. These tests deliberately
# cover the NON-redundant slice: how the ``_cmd_mirror`` entry point routes
# observation results and propagates ``MirrorError`` into process exits -- the
# dispatch/error-shape seam, not the git plumbing.


class TestMirrorCmdConvergedShortCircuit:
    """A converged plan must short-circuit: print 'Already converged' and never
    enter the convergence path."""

    def test_converged_plan_does_not_converge(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        _mirror_workspace(mock_git_repo, tmp_path)
        monkeypatch.setattr(
            mirror_mod, "validate_subtree_remote_ssh_host", lambda remote, root: None
        )
        plan = MirrorPlan(
            state="converged",
            split_sha="a" * 40,
            remote_tip="b" * 40,
            split_ancestry_sha="a" * 40,
        )
        monkeypatch.setattr(mirror_mod, "observe", lambda remote, root, path: plan)
        converge_calls = {"n": 0}
        monkeypatch.setattr(
            mirror_mod,
            "_converge",
            lambda *a, **k: converge_calls.__setitem__("n", converge_calls["n"] + 1),
        )

        _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)

        assert converge_calls["n"] == 0
        assert "Already converged" in capsys.readouterr().out


class TestMirrorCmdContractViolationRefuses:
    """A contract-violated plan must refuse to write and exit 1 with the
    remediation guidance -- without invoking convergence."""

    def test_contract_violation_exits_without_converging(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        _mirror_workspace(mock_git_repo, tmp_path)
        monkeypatch.setattr(
            mirror_mod, "validate_subtree_remote_ssh_host", lambda remote, root: None
        )
        plan = MirrorPlan(
            state="contract_violated",
            split_sha="a" * 40,
            remote_tip="c" * 40,
            split_ancestry_sha="a" * 40,
            foreign_commits=[("d" * 40, ["src/hand_authored.py"])],
        )
        monkeypatch.setattr(mirror_mod, "observe", lambda remote, root, path: plan)
        converge_calls = {"n": 0}
        monkeypatch.setattr(
            mirror_mod,
            "_converge",
            lambda *a, **k: converge_calls.__setitem__("n", converge_calls["n"] + 1),
        )

        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)

        assert exc_info.value.code == 1
        assert converge_calls["n"] == 0
        err = capsys.readouterr().err
        assert "contract-violated" in err
        assert "src/hand_authored.py" in err


class TestMirrorCmdObserveErrorPropagation:
    """A ``MirrorError`` raised during observation (e.g. subtree split failure)
    must become a clean exit-1 with the message on stderr -- not a traceback."""

    def test_subtree_split_failure_propagates_as_exit(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        _mirror_workspace(mock_git_repo, tmp_path)
        monkeypatch.setattr(
            mirror_mod, "validate_subtree_remote_ssh_host", lambda remote, root: None
        )

        orig_git = mirror_mod._git

        def fake_git(args, cwd=None, timeout=180):
            if list(args[:2]) == ["subtree", "split"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="fatal: not a valid object"
                )
            return orig_git(args, cwd=cwd, timeout=timeout)

        monkeypatch.setattr(mirror_mod, "_git", fake_git)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "git subtree split failed" in err


class TestMirrorCmdConvergeErrorPropagation:
    """A ``MirrorError`` raised during convergence (e.g. push rejected) must
    become a clean exit-1 with the message on stderr."""

    def test_push_failure_during_converge_propagates_as_exit(self, mock_git_repo, tmp_path, monkeypatch, capsys):
        _mirror_workspace(mock_git_repo, tmp_path)
        monkeypatch.setattr(
            mirror_mod, "validate_subtree_remote_ssh_host", lambda remote, root: None
        )
        # Virgin plan -> convergence needs a split push, which we make fail.
        plan = MirrorPlan(state="virgin", split_sha="a" * 40)
        monkeypatch.setattr(mirror_mod, "observe", lambda remote, root, path: plan)

        def boom(remote, split_sha, expected_tip, root):
            raise MirrorError(
                "failed to push split to mirror: remote: Permission denied"
            )

        monkeypatch.setattr(mirror_mod, "_push_bare_split", boom)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_mirror({"project": "mylib"}, project_root=mock_git_repo)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "failed to push split to mirror" in err


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
             patch("rlsbl.workspace.load_workspace", return_value=[]), \
             patch("rlsbl.workspace.load_releasables", return_value=[]), \
             patch("builtins.open", mock_open()), \
             patch("os.rename"), \
             patch("os.unlink"):
            edit_release_run_cmd(["1.0.0"], {}, project_root=".")

        # Should use monorepo tag format
        target.monorepo_tag_format.assert_called_once_with(
            "my-pkg", "1.0.0", path="packages/my-pkg"
        )
        assert any(c[0] == (["release", "view", "my-pkg@v1.0.0"],) for c in mock_run_gh.call_args_list)
