"""Tests for monorepo-aware lock file placement."""

import fcntl
import json
import os
import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.lock import acquire_lock, release_lock, rlsbl_lock
from rlsbl.release_file import ReleaseConfig


def _rc(bump="patch", include=None, exclude=None):
    """Shorthand for creating a ReleaseConfig with sensible defaults."""
    return ReleaseConfig(
        bump=bump,
        include=include or ["npm"],
        exclude=exclude or [],
    )


class TestLockDirParameter:
    """Test that acquire_lock/release_lock respect the lock_dir parameter."""

    def test_acquire_lock_default_dir(self, tmp_path, monkeypatch):
        """Default lock goes to .rlsbl/lock."""
        monkeypatch.chdir(tmp_path)

        acquire_lock(project_root=str(tmp_path))
        try:
            assert (tmp_path / ".rlsbl" / "lock").exists()
        finally:
            release_lock()

    def test_acquire_lock_custom_dir(self, tmp_path, monkeypatch):
        """Passing lock_dir='.rlsbl-monorepo' creates lock in .rlsbl-monorepo/lock."""
        monkeypatch.chdir(tmp_path)

        acquire_lock(lock_dir=".rlsbl-monorepo", project_root=str(tmp_path))
        try:
            assert (tmp_path / ".rlsbl-monorepo" / "lock").exists()
            # Must NOT create .rlsbl/ at all
            assert not (tmp_path / ".rlsbl").exists()
        finally:
            release_lock()

    def test_release_lock_cleans_up_custom_dir(self, tmp_path, monkeypatch):
        """Releasing a custom-dir lock removes the correct file."""
        monkeypatch.chdir(tmp_path)

        acquire_lock(lock_dir=".rlsbl-monorepo", project_root=str(tmp_path))
        lock_path = tmp_path / ".rlsbl-monorepo" / "lock"
        assert lock_path.exists()

        release_lock()
        assert not lock_path.exists()

    def test_context_manager_custom_dir(self, tmp_path, monkeypatch):
        """rlsbl_lock context manager passes through lock_dir."""
        monkeypatch.chdir(tmp_path)

        with rlsbl_lock(lock_dir=".rlsbl-monorepo", project_root=str(tmp_path)):
            lock_path = tmp_path / ".rlsbl-monorepo" / "lock"
            assert lock_path.exists()

            # Lock should be held inside the context
            fd = open(str(lock_path), "w")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert False, "Lock should be held inside context manager"
            except (OSError, BlockingIOError):
                pass
            finally:
                fd.close()

        # Lock file removed after exit
        assert not lock_path.exists()


class TestMonorepoReleaseLockPlacement:
    """Integration: verify monorepo release uses .rlsbl-monorepo/ for locks."""

    def _setup_monorepo(self, repo_root, project_name="tooling",
                        project_path="tooling", version="1.0.0"):
        """Create a monorepo workspace structure inside repo_root."""
        ws_dir = repo_root / ".rlsbl-monorepo"
        ws_dir.mkdir(exist_ok=True)
        (ws_dir / "workspace.toml").write_text(
            f'[[projects]]\npath = "{project_path}"\nname = "{project_name}"\n'
        )

        proj_dir = repo_root / project_path
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "package.json").write_text(
            json.dumps({"name": project_name, "version": version}, indent=2) + "\n"
        )
        (proj_dir / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {version}\n\n"
            "Patch release with bugfixes and improvements.\n"
        )
        changes_dir = proj_dir / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        (proj_dir / ".rlsbl" / "config.json").write_text(
            json.dumps({"private": False}) + "\n"
        )

        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add monorepo structure"],
            cwd=str(repo_root), check=True,
        )
        return proj_dir

    @patch("rlsbl.commands.release.push_if_needed")
    @patch("rlsbl.commands.release.commit_files", return_value=True)
    @patch("rlsbl.commands.release.run")
    @patch("rlsbl.commands.release.get_current_branch", return_value="main")
    @patch("rlsbl.commands.release.is_clean_tree", return_value=True)
    @patch("rlsbl.commands.release.check_gh_auth", return_value=True)
    @patch("rlsbl.commands.release.check_gh_installed", return_value=True)
    @patch("rlsbl.commands.release.generate_changelog")
    @patch("rlsbl.commands.release.validate_unreleased", return_value={"passed": True, "checks": {}})
    def test_monorepo_release_uses_monorepo_lock_dir(
        self, _validate, _gen_cl, _gh_inst, _gh_auth, _clean, _branch, mock_run,
        _commit_files, _push,
        mock_git_repo,
        monkeypatch,
    ):
        """Monorepo release puts lock in .rlsbl-monorepo/, not .rlsbl/ at repo root."""
        from rlsbl.commands.release import run_cmd

        proj_dir = self._setup_monorepo(mock_git_repo, "tooling", "tooling")
        monkeypatch.chdir(str(proj_dir))

        lock_acquired_in = []

        original_acquire = acquire_lock

        def spy_acquire(lock_dir=".rlsbl", project_root=None):
            lock_acquired_in.append(lock_dir)
            return original_acquire(lock_dir=lock_dir, project_root=project_root)

        def mock_run_side_effect(cmd, args, **kwargs):
            if "rev-list" in args:
                return "0"
            if "rev-parse" in args and "HEAD" in args:
                return "abc123"
            return ""

        mock_run.side_effect = mock_run_side_effect

        with patch("rlsbl.commands.release.acquire_lock", spy_acquire):
            with patch("sys.stdout", new_callable=StringIO):
                run_cmd(_rc(), {"yes": True, "quiet": False}, project_root=".", monorepo_root=str(mock_git_repo))

        assert lock_acquired_in == [".rlsbl-monorepo"]
        # .rlsbl/ should NOT exist at the repo root (only .rlsbl-monorepo/ should)
        assert not (mock_git_repo / ".rlsbl").is_dir(), \
            "Monorepo release should not create .rlsbl/ at repo root"
