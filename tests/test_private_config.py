"""Tests for the required ``private`` config key in the release flow.

Verifies:
- Release aborts when ``private`` key is missing from config.
- Release aborts when ``private: true`` and a target has ``publish.<target>.local: true``.
- Release proceeds when ``private: true`` with no local publish config.
- Release proceeds when ``private: false`` (normal case).
- When ``private: true``, ``target.publish()`` is not called.
- When ``private: true`` with ``assets: true``, asset upload still runs.
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

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
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"targets": ["npm"]}))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert '"private" key missing' in captured.err

    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    def test_release_aborts_when_private_true_with_local_publish(
        self, _gh_inst, _gh_auth, capsys
    ):
        """Release exits when private=true and a target has publish.local=true."""
        _write_config(self.tmp_dir, {
            "private": True,
            "publish": {"npm": {"local": True}},
        })

        from rlsbl.commands.release import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(_rc(), {"quiet": True}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"private": True, "publish": {"npm": {"local": True}}}))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "private repo cannot publish" in captured.err

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_release_proceeds_when_private_true_no_local_publish(
        self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch,
        _commit_files, mock_run, _push,
    ):
        """Release does not abort when private=true and no publish.local config."""
        _write_config(self.tmp_dir, {"private": True})

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        with patch("sys.stdout", new_callable=StringIO):
            # Should not raise SystemExit
            run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"private": True}))

    @patch("rlsbl.commands.release.push_if_needed")
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
        _commit_files, mock_run, _push,
    ):
        """Release does not abort when private=false (normal public repo)."""
        _write_config(self.tmp_dir, {"private": False})

        mock_run.side_effect = ["", "0", "v1.0.0", "", "", ""]

        from rlsbl.commands.release import run_cmd

        with patch("sys.stdout", new_callable=StringIO):
            # Should not raise SystemExit
            run_cmd(_rc(), {"dry-run": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"private": False}))


class TestPrivatePublishGuardrail:
    """Tests that private repos skip target.publish() but still allow asset upload."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
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

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
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
    def test_private_true_skips_publish(
        self, _changes_dir, _extract, _finalize, _gen_ver_file,
        _validate, _gen_cl, _deploy, _tag, _gh_inst, _gh_auth,
        _clean, _branch, _commit_files, mock_run, _push, _lock, _unlock,
    ):
        """When private=true, target.publish() is not called."""
        _write_config(self.tmp_dir, {"private": True})

        from rlsbl.commands.release import run_cmd
        from rlsbl.targets.npm import NpmTarget

        mock_run.side_effect = [
            # run_cmd phase:
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "v1.0.0",           # git tag -l v1.0.0 (exists -> bump)
            "",                 # git tag -l v1.0.1 (doesn't exist)
            "",                 # git status --porcelain (pre-hook snapshot)
            "",                 # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "",                 # git status --porcelain (re-check guard)
            "abc123",           # git rev-parse HEAD (pre_release_sha)
            "",                 # git tag v1.0.1
            "",                 # git push origin v1.0.1
            "def456",           # git rev-parse HEAD (pushed_sha)
            "",                 # gh release create ...
        ]

        with patch("sys.stdout", new_callable=StringIO):
            with patch.object(NpmTarget, "publish") as mock_publish:
                run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"private": True}))
                # publish() must NOT be called for private repos
                mock_publish.assert_not_called()

    @patch("rlsbl.commands.release.release_lock")
    @patch("rlsbl.commands.release.acquire_lock")
    @patch("rlsbl.commands.release.push_if_needed")
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
    def test_private_true_with_assets_still_uploads(
        self, mock_upload_assets,
        _changes_dir, _extract, _finalize, _gen_ver_file,
        _validate, _gen_cl, _deploy, _tag, _gh_inst, _gh_auth,
        _clean, _branch, _commit_files, mock_run, _push, _lock, _unlock,
    ):
        """When private=true with assets: true, asset upload still runs."""
        _write_config(self.tmp_dir, {
            "private": True,
            "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}},
        })

        from rlsbl.commands.release import run_cmd

        mock_run.side_effect = [
            # run_cmd phase:
            "",                 # git fetch origin --quiet
            "0",                # git rev-list --count HEAD..origin/main
            "v1.0.0",           # git tag -l v1.0.0 (exists -> bump)
            "",                 # git tag -l v1.0.1 (doesn't exist)
            "",                 # git status --porcelain (pre-hook snapshot)
            "",                 # git status --porcelain (post-hook snapshot)
            # _run_release_mutating phase:
            "",                 # git status --porcelain (baseline snapshot)
            "/tmp/fake-repo",   # git rev-parse --show-toplevel (for vpath)
            "",                 # git status --porcelain (re-check guard)
            "abc123",           # git rev-parse HEAD (pre_release_sha)
            "",                 # git tag v1.0.1
            "",                 # git push origin v1.0.1
            "def456",           # git rev-parse HEAD (pushed_sha)
            "",                 # gh release create ...
        ]

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd(_rc(), {"yes": True, "quiet": False}, ctx=ProjectContext(project_root=Path("."), monorepo_root=None, config={"private": True, "publish": {"npm": {"assets": True, "max_asset_size_mb": 50}}}))

        # upload_release_assets must be called even for private repos
        mock_upload_assets.assert_called_once()
