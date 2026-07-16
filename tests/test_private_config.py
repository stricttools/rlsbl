"""Tests for the required ``private`` config key in the release flow.

Verifies:
- Release aborts when ``private`` key is missing from config.
- Release aborts when ``private: true`` and a pipeline has ``local: true``.
- Release proceeds when ``private: true`` with no local pipeline config.
- Release proceeds when ``private: false`` (normal case).
- When ``private: true``, pipeline.publish() is not called.
- When ``private: true`` with ``assets: true``, asset upload still runs.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import rlsbl.lock
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


def _write_config(tmp_dir, config):
    """Write .rlsbl/config.json in tmp_dir with the given dict."""
    rlsbl_dir = os.path.join(tmp_dir, ".rlsbl")
    os.makedirs(rlsbl_dir, exist_ok=True)
    with open(os.path.join(rlsbl_dir, "config.json"), "w") as f:
        json.dump(config, f)


class TestPrivateConfigRequired:
    """Tests that release enforces the ``private`` config key."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rlsbl.lock, "_lock_fd", None)
        self.tmp_dir = str(tmp_path)
        # Minimal npm project
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')

    def test_release_aborts_when_private_key_missing(self, capsys):
        """Release exits with error when ``private`` is absent from config."""
        # No config.json at all, or config without "private"
        _write_config(self.tmp_dir, {"targets": ["npm"]})

        from rlsbl.commands.release import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"targets": ["npm"]}))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "publish_mode" in captured.err

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    def test_release_aborts_when_private_true_with_local_pipeline(
        self, _gh_inst, _gh_auth, capsys
    ):
        """Release exits when private=true and a pipeline has local=true."""
        _write_config(self.tmp_dir, {
            "publish_mode": "none",
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": True}},
        })

        from rlsbl.commands.release import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "none", "pipelines": {"npm": {"type": "npm", "local": True}}}))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "cannot publish to public registries" in captured.err

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
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_proceeds_when_private_true_no_local_pipeline(
        self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch,
        _commit_files, mock_run, _tag_local, _push, _remote_exists, capsys,
    ):
        """Release does not abort when private=true and no local pipeline config."""
        _write_config(self.tmp_dir, {"publish_mode": "none", "targets": ["npm"], "pipelines": {}})

        mock_run.side_effect = ["", "0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        # Should not raise SystemExit
        run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "none", "pipelines": {}}))

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
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_proceeds_when_private_false(
        self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch,
        _commit_files, mock_run, _tag_local, _push, _remote_exists, capsys,
    ):
        """Release does not abort when private=false (normal public repo)."""
        _write_config(self.tmp_dir, {"publish_mode": "ci", "targets": ["npm"], "pipelines": {}})

        mock_run.side_effect = ["", "0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        # Should not raise SystemExit
        run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "ci", "pipelines": {}}))


class TestPrivatePublishGuardrail:
    """Tests that private repos skip pipeline.publish() but still allow asset upload."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rlsbl.lock, "_lock_fd", None)
        self.tmp_dir = str(tmp_path)
        # Minimal npm project
        with open("package.json", "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f, indent=2)
            f.write("\n")
        with open("CHANGELOG.md", "w") as f:
            f.write("# Changelog\n\n## 1.0.1\n\nPatch release.\n")
        os.makedirs(os.path.join(".rlsbl", "changes"), exist_ok=True)
        with open(os.path.join(".rlsbl", "changes", "unreleased.jsonl"), "w") as f:
            f.write('{"commits":["abc1234"],"user_facing":true,"description":"Bugfix","type":"fix"}\n')

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally", side_effect=[True, False, False])
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Bugfix")
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    @patch("rlsbl.app.run_checks", return_value=([], [], 0))
    def test_private_true_skips_publish(
        self, _run_checks, _vrt,
        _changes_dir, _extract, _finalize, _gen_ver_file,
        _validate, _gen_cl, _deploy, _tag, _gh_inst, _gh_auth,
        _clean, _branch, _commit_files, mock_run, _tag_local, _tag_remote,
        _run_gh, _push, _lock, _unlock,
        _remote_exists, capsys,
    ):
        """When private=true, pipeline.publish() is not called."""
        _write_config(self.tmp_dir, {"publish_mode": "none", "targets": ["npm"], "pipelines": {"npm": {"type": "npm", "local": False}}})

        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            # run_cmd phase:
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "",                 # git status --porcelain (pre-hook snapshot)
            "",                 # git status --porcelain (pre-selfdoc snapshot)
            "",                 # git status --porcelain (post-selfdoc snapshot)
            "",                 # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123",           # git rev-parse HEAD (pre_release_sha)
            "",                 # git status --porcelain (re-check guard)
            "",                 # git log -1 --format=%s (COMMITTED guard)
            "",                 # git status --porcelain (backfilled .md detection)
            "",                 # git tag v1.0.1
            "abc123",           # rev-parse HEAD (PUSHED guard _local_head)
            "abc123",           # rev-parse origin/main (PUSHED guard _remote_head)
            "",                 # git push origin v1.0.1
            "def456",           # git rev-parse HEAD (pushed_sha)
        ]

        with patch("rlsbl.pipelines.npm.NpmPipeline.publish") as mock_publish:
            run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "none", "pipelines": {"npm": {"type": "npm", "local": False}}}))
            # publish() must NOT be called for private repos
            mock_publish.assert_not_called()

    @patch("rlsbl.commands.release.remote_branch_exists", return_value=True)
    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run_gh", return_value="")
    @patch("rlsbl.commands.release.tag_exists_on_remote", return_value=False)
    @patch("rlsbl.commands.release.tag_exists_locally")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.should_tag", return_value=False)
    @patch("rlsbl.commands.release.read_deploy_config", return_value=([], []))
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    @patch("rlsbl.commands.release.generate_version_file")
    @patch("rlsbl.commands.release.finalize_version")
    @patch("rlsbl.commands.release.extract_changelog_entry", return_value="- Bugfix")
    @patch("rlsbl.commands.release.get_changes_dir", return_value=".rlsbl/changes")
    @patch("rlsbl.commands.release.upload_release_assets")
    @patch("rlsbl.commands.release.validate_release_targets", return_value="npm")
    @patch("rlsbl.app.run_checks", return_value=([], [], 0))
    def test_private_true_with_assets_still_uploads(
        self, _run_checks, _vrt, mock_upload_assets,
        _changes_dir, _extract, _finalize, _gen_ver_file,
        _validate, _gen_cl, _deploy, _tag, _gh_inst, _gh_auth,
        _clean, _branch, _commit_files, mock_run, mock_tag_local, _tag_remote,
        _run_gh, _push, _lock, _unlock,
        _remote_exists, capsys,
    ):
        """When private=true with assets: true, asset upload still runs."""
        _write_config(self.tmp_dir, {
            "publish_mode": "none",
            "targets": ["npm"],
            "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}},
        })

        from rlsbl.commands.release import run_cmd

        mock_tag_local.side_effect = [True, False, False]
        mock_run.side_effect = [
            # run_cmd phase:
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "",                 # git status --porcelain (pre-hook snapshot)
            "",                 # git status --porcelain (pre-selfdoc snapshot)
            "",                 # git status --porcelain (post-selfdoc snapshot)
            "",                 # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "abc123",           # git rev-parse HEAD (pre_release_sha)
            "",                 # git status --porcelain (re-check guard)
            "",                 # git log -1 --format=%s (COMMITTED guard)
            "",                 # git status --porcelain (backfilled .md detection)
            "",                 # git tag v1.0.1
            "abc123",           # rev-parse HEAD (PUSHED guard _local_head)
            "abc123",           # rev-parse origin/main (PUSHED guard _remote_head)
            "",                 # git push origin v1.0.1
            "def456",           # git rev-parse HEAD (pushed_sha)
        ]

        run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), workspace_root=None, config={"publish_mode": "none", "pipelines": {"npm": {"type": "npm", "local": False, "assets": True, "max_asset_size_mb": 50}}}))

        # upload_release_assets must be called even for private repos
        mock_upload_assets.assert_called_once()
