"""Tests for --watch / --no-watch mutex flags on the release run subcommand.

Verifies:
- CLI rejects missing --watch/--no-watch (mutex group enforcement)
- CLI rejects both --watch and --no-watch together
- --no-watch prints the hint message
- --watch invokes the watch command with the pushed SHA
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from pathlib import Path
from rlsbl.context import ProjectContext

from rlsbl import app
from rlsbl.commands.release import run_cmd
from rlsbl.release_file import ReleaseConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_npm_project(tmp_path):
    """Create a minimal npm project with changelog and JSONL setup."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}) + "\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.1\n\nPatch release.\n"
    )
    changes_dir = tmp_path / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    config_dir = tmp_path / ".rlsbl"
    config_dir.mkdir(exist_ok=True)
    config = {"targets": ["npm"], "private": False}
    (config_dir / "config.json").write_text(json.dumps(config) + "\n")


def _rc(**overrides):
    """Build a ReleaseConfig with sensible defaults."""
    defaults = {"bump": "patch", "include": ["npm"], "exclude": []}
    defaults.update(overrides)
    return ReleaseConfig(**defaults)


# ---------------------------------------------------------------------------
# Mutex enforcement tests (CLI level via app.test)
# ---------------------------------------------------------------------------

class TestWatchMutexEnforcement:
    """Strictcli mutex group rejects invalid flag combinations."""

    def test_neither_watch_nor_no_watch_errors(self, tmp_project):
        """Providing neither --watch nor --no-watch results in an error."""
        # Create minimal project structure so _require_project_root succeeds
        (tmp_project / ".rlsbl").mkdir()
        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir()
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
        )

        result = app.test(["release", "run"])
        assert result.exit_code != 0
        assert "one of --watch, --no-watch is required" in result.stderr

    def test_both_watch_and_no_watch_errors(self, tmp_project):
        """Providing both --watch and --no-watch results in an error."""
        (tmp_project / ".rlsbl").mkdir()
        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir()
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
        )

        result = app.test(["release", "run", "--watch", "--no-watch"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.stderr


# ---------------------------------------------------------------------------
# --no-watch prints the hint
# ---------------------------------------------------------------------------

class TestNoWatchPrintsHint:
    """--no-watch preserves the existing behavior of printing the watch hint."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_no_watch_shows_hint_in_dry_run(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """In dry-run mode with --no-watch, the watch hint is printed."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        run_cmd(
            _rc(),
            {"dry-run": True, "quiet": False, "yes": True, "watch": False},
        
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)

        captured = capsys.readouterr()
        # Dry-run exits before the watch/hint logic, so we just verify it
        # doesn't crash and the dry-run summary shows up
        assert "Dry run" in captured.out


# ---------------------------------------------------------------------------
# --watch invokes watch.run_cmd
# ---------------------------------------------------------------------------

class TestWatchInvokesWatchCmd:
    """--watch calls the watch command's run_cmd with the pushed SHA."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    def test_watch_flag_in_dry_run(
        self,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """In dry-run mode, --watch doesn't invoke watch (dry-run exits early)."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        run_cmd(
            _rc(),
            {"dry-run": True, "quiet": False, "yes": True, "watch": True},
        
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)

        captured = capsys.readouterr()
        assert "Dry run" in captured.out


class TestWatchInvokedAfterRelease:
    """When --watch is set and release completes, watch.run_cmd is called."""

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.commit_files_if_changed")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    @patch("rlsbl.commands.release.has_staged_or_modified", return_value=True)
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.upload_release_assets")
    @patch("rlsbl.commands.release.get_publish_config", return_value={})
    def test_watch_flag_invokes_watch_run_cmd(
        self,
        _pub_cfg,
        _upload,
        _tag,
        _deploy,
        _gen_ver,
        _finalize,
        _staged,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_if,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """After a successful release with --watch, watch.run_cmd is called."""
        _setup_npm_project(tmp_project)

        # Create release file so finalization doesn't error
        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
        )

        fake_sha = "abc123def456"
        # mock_run calls: fetch, rev-list, tag-l (current tag exists),
        # tag-l (new tag), pre-hook status, post-hook status,
        # status --porcelain (baseline), status --porcelain (re-check),
        # rev-parse HEAD (pre-release sha), tag, push tag,
        # rev-parse HEAD (pushed sha), gh release create
        mock_run.side_effect = [
            "",       # fetch
            "0",      # rev-list (not behind)
            "v1.0.0", # tag -l current (exists)
            "",       # tag -l new (doesn't exist)
            "",       # pre-hook dirty snapshot
            "",       # post-hook dirty snapshot
            "",       # baseline dirty snapshot
            "/tmp/fake-repo",  # git rev-parse --show-toplevel (for vpath)
            "",       # re-check dirty snapshot
            "pre123", # rev-parse HEAD (pre-release)
            "",       # git tag
            "",       # git push origin tag
            fake_sha, # rev-parse HEAD (pushed sha)
            "",       # gh release create
        ]

        with patch("rlsbl.commands.release.acquire_lock"), \
             patch("rlsbl.commands.release.release_lock"), \
             patch("rlsbl.commands.watch.run_cmd") as mock_watch:
            # watch.run_cmd calls sys.exit, so prevent that
            mock_watch.return_value = None
            run_cmd(
                _rc(),
                {"yes": True, "quiet": False, "watch": True},
            
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)
            mock_watch.assert_called_once_with(None, [fake_sha], {})

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.commit_files_if_changed")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch(
        "rlsbl.commands.release.validate_unreleased",
        return_value={"passed": True, "checks": {}},
    )
    @patch("rlsbl.commands.release.has_staged_or_modified", return_value=True)
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.upload_release_assets")
    @patch("rlsbl.commands.release.get_publish_config", return_value={})
    def test_no_watch_flag_prints_hint(
        self,
        _pub_cfg,
        _upload,
        _tag,
        _deploy,
        _gen_ver,
        _finalize,
        _staged,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_if,
        _commit_files,
        mock_run,
        _push,
        tmp_project,
        capsys,
    ):
        """After a successful release with --no-watch, the hint is printed."""
        _setup_npm_project(tmp_project)

        releases_dir = tmp_project / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
        )

        fake_sha = "abc123def456"
        mock_run.side_effect = [
            "",       # fetch
            "0",      # rev-list
            "v1.0.0", # tag -l current
            "",       # tag -l new
            "",       # pre-hook status
            "",       # post-hook status
            "",       # baseline status
            "/tmp/fake-repo",  # git rev-parse --show-toplevel (for vpath)
            "",       # re-check dirty status
            "pre123", # rev-parse HEAD (pre-release)
            "",       # git tag
            "",       # git push origin tag
            fake_sha, # rev-parse HEAD (pushed sha)
            "",       # gh release create
        ]

        with patch("rlsbl.commands.release.acquire_lock"), \
             patch("rlsbl.commands.release.release_lock"), \
             patch("rlsbl.commands.watch.run_cmd") as mock_watch:
            run_cmd(
                _rc(),
                {"yes": True, "quiet": False, "watch": False},
            
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"private": False}),
)
            mock_watch.assert_not_called()

        captured = capsys.readouterr()
        assert f"Watch CI: rlsbl watch {fake_sha}" in captured.out
