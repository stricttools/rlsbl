"""Tests for --watch / --no-watch flags on the release run subcommand.

Verifies:
- --no-watch prints the hint message
- --watch invokes the watch command with the pushed SHA
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from pathlib import Path
from rlsbl.context import ProjectContext

import rlsbl.lock
from rlsbl import app
from rlsbl.commands.release import run_cmd
from rlsbl.release_file import ReleaseConfig


@pytest.fixture(autouse=True)
def _reset_lock_fd(monkeypatch):
    """Prevent cross-test leakage of the advisory lock file descriptor."""
    monkeypatch.setattr(rlsbl.lock, "_lock_fd", None)


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
    config = {"targets": ["npm"], "publish_mode": "ci"}
    (config_dir / "config.json").write_text(json.dumps(config) + "\n")


def _rc(**overrides):
    """Build a ReleaseConfig with sensible defaults."""
    defaults = {"bump": "patch", "include": ["npm"], "exclude": []}
    defaults.update(overrides)
    return ReleaseConfig(**defaults)


# ---------------------------------------------------------------------------
# --no-watch prints the hint
# ---------------------------------------------------------------------------

class TestNoWatchPrintsHint:
    """--no-watch preserves the existing behavior of printing the watch hint."""

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
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
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    def test_no_watch_shows_hint_in_dry_run(
        self,
        _vrt,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _run_gh,
        _push,
        _remote_exists,
        tmp_project,
        capsys,
    ):
        """In dry-run mode with --no-watch, the watch hint is printed."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "", ""]

        run_cmd(
            _rc(),
            {"dry-run": True, "quiet": False, "yes": True, "watch": False},
        
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
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

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False])
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
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    def test_watch_flag_in_dry_run(
        self,
        _vrt,
        _validate,
        _gen_cl,
        _gh_inst,
        _gh_auth,
        _clean,
        _branch,
        _commit_files,
        mock_run,
        _tag_local,
        _run_gh,
        _push,
        _remote_exists,
        tmp_project,
        capsys,
    ):
        """In dry-run mode, --watch doesn't invoke watch (dry-run exits early)."""
        _setup_npm_project(tmp_project)
        mock_run.side_effect = ["", "0", "", ""]

        run_cmd(
            _rc(),
            {"dry-run": True, "quiet": False, "yes": True, "watch": True},
        
            ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
)

        captured = capsys.readouterr()
        assert "Dry run" in captured.out


class TestWatchInvokedAfterRelease:
    """When --watch is set and release completes, watch.run_cmd is called."""

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False, False])
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
    @patch("rlsbl.app.run_checks", return_value=([], 0))
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    def test_watch_flag_invokes_watch_run_cmd(
        self,
        _vrt,
        _run_checks,
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
        _tag_local,
        _tag_remote,
        _run_gh,
        _push,
        _remote_exists,
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
        mock_run.side_effect = [
            "",       # fetch
            "0",      # rev-list (not behind)
            "",       # pre-hook dirty snapshot
            "",       # pre-selfdoc dirty snapshot
            "",       # post-selfdoc dirty snapshot
            "",       # post-hook dirty snapshot
            "",       # baseline dirty snapshot
            "/tmp/fake-repo",  # git rev-parse --show-toplevel (for vpath)
            "",       # re-check dirty snapshot
            "pre123", # rev-parse HEAD (pre-release)
            "",       # git log -1 (COMMITTED guard)
            "",       # status --porcelain (backfilled .md detection)
            "",       # git tag (create tag)
            "pre123", # rev-parse HEAD (PUSHED guard _local_head)
            "pre123", # rev-parse origin/main (PUSHED guard _remote_head)
            "",       # git push origin tag
            fake_sha, # rev-parse HEAD (pushed sha)
        ]

        with patch("rlsbl.commands.release.acquire_lock"), \
             patch("rlsbl.commands.release.release_lock"), \
             patch("rlsbl.commands.watch.run_cmd") as mock_watch:
            # watch.run_cmd calls sys.exit, so prevent that
            mock_watch.return_value = None
            run_cmd(
                _rc(),
                {"yes": True, "quiet": False, "watch": True},
            
                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
)
            mock_watch.assert_called_once_with(None, [fake_sha], {})

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False, False])
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
    @patch("rlsbl.app.run_checks", return_value=([], 0))
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    def test_no_watch_flag_prints_hint(
        self,
        _vrt,
        _run_checks,
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
        _tag_local,
        _tag_remote,
        _run_gh,
        _push,
        _remote_exists,
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
            "",       # pre-hook status
            "",       # pre-selfdoc status
            "",       # post-selfdoc status
            "",       # post-hook status
            "",       # baseline status
            "/tmp/fake-repo",  # git rev-parse --show-toplevel (for vpath)
            "",       # re-check dirty status
            "pre123", # rev-parse HEAD (pre-release)
            "",       # git log -1 (COMMITTED guard)
            "",       # status --porcelain (backfilled .md detection)
            "",       # git tag (create tag)
            "pre123", # rev-parse HEAD (PUSHED guard _local_head)
            "pre123", # rev-parse origin/main (PUSHED guard _remote_head)
            "",       # git push origin tag
            fake_sha, # rev-parse HEAD (pushed sha)
        ]

        with patch("rlsbl.commands.release.acquire_lock"), \
             patch("rlsbl.commands.release.release_lock"), \
             patch("rlsbl.commands.watch.run_cmd") as mock_watch:
            run_cmd(
                _rc(),
                {"yes": True, "quiet": False, "watch": False},

                ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}),
)
            mock_watch.assert_not_called()

        captured = capsys.readouterr()
        assert f"Watch CI: rlsbl watch {fake_sha}" in captured.out
