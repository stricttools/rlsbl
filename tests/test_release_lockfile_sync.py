"""Tests for lockfile auto-sync during release (_sync_lockfiles)."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from rlsbl.commands.release import _sync_lockfiles

_real_stat = os.stat


def _make_subprocess_run_side_effect(sync_result=None, gitignored=False):
    """Build a side_effect for subprocess.run that handles both sync and git check-ignore calls.

    sync_result: value to return for the sync command (e.g. CompletedProcess), or
                 an exception to raise.
    gitignored: if True, git check-ignore returns 0 (file is ignored);
                if False, returns 1 (file is NOT ignored).
    """
    if sync_result is None:
        sync_result = subprocess.CompletedProcess(args=[], returncode=0)

    def side_effect(cmd, **kwargs):
        if cmd[0] == "git" and cmd[1] == "check-ignore":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0 if gitignored else 1,
            )
        if isinstance(sync_result, Exception):
            raise sync_result
        return sync_result

    return side_effect


def _make_stat_interceptor(lockfile_name, mtime_before, mtime_after):
    """Return an os.stat side_effect function that fakes mtime_ns for a lockfile.

    os.path.exists also calls os.stat internally (consuming one call), so the
    interceptor must account for 3 calls per lockfile: exists check, explicit
    "before" stat, and explicit "after" stat. The mtime_before value is
    returned on the second call; mtime_after on the third and beyond.
    """
    call_count = 0

    def fake_stat(path, *args, **kwargs):
        nonlocal call_count
        if str(path).endswith(lockfile_name):
            call_count += 1
            mock_result = MagicMock()
            # Call 1: os.path.exists (mtime doesn't matter, just needs to not raise)
            # Call 2: mtime_before
            # Call 3+: mtime_after
            if call_count <= 2:
                mock_result.st_mtime_ns = mtime_before
            else:
                mock_result.st_mtime_ns = mtime_after
            return mock_result
        return _real_stat(path, *args, **kwargs)

    return fake_stat


class TestSyncLockfiles:
    """Unit tests for _sync_lockfiles helper."""

    def test_detects_uv_lock_and_runs_uv_lock(self, tmp_path):
        """When uv.lock exists and uv is on PATH, runs 'uv lock'."""
        (tmp_path / "uv.lock").write_text("old content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("uv.lock", 1000, 2000)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        # Sync command should have been called
        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 1
        assert sync_calls[0] == (
            (["uv", "lock"],),
            {"cwd": str(tmp_path), "timeout": 30, "check": True, "capture_output": True},
        )

        expected = os.path.normpath(os.path.join(str(tmp_path), "uv.lock"))
        assert expected in files_to_commit

    def test_detects_package_lock_and_runs_npm(self, tmp_path):
        """When package-lock.json exists and npm is on PATH, runs npm install --package-lock-only."""
        (tmp_path / "package-lock.json").write_text("{}\n")
        target_paths = {"npm": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("package-lock.json", 100, 200)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/npm"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 1
        assert sync_calls[0] == (
            (["npm", "install", "--package-lock-only"],),
            {"cwd": str(tmp_path), "timeout": 30, "check": True, "capture_output": True},
        )

        expected = os.path.normpath(os.path.join(str(tmp_path), "package-lock.json"))
        assert expected in files_to_commit

    def test_detects_go_sum_and_runs_go_mod_tidy(self, tmp_path):
        """When go.sum exists and go is on PATH, runs go mod tidy."""
        (tmp_path / "go.sum").write_text("hash\n")
        target_paths = {"go": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("go.sum", 100, 200)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/go"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 1
        assert sync_calls[0] == (
            (["go", "mod", "tidy"],),
            {"cwd": str(tmp_path), "timeout": 30, "check": True, "capture_output": True},
        )

        expected = os.path.normpath(os.path.join(str(tmp_path), "go.sum"))
        assert expected in files_to_commit

    def test_missing_tool_warns_and_continues(self, tmp_path):
        """When the sync tool is not on PATH, logs a warning and skips."""
        (tmp_path / "uv.lock").write_text("content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        with (
            patch("rlsbl.commands.release.shutil.which", return_value=None),
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        mock_run.assert_not_called()

        log.assert_called()
        warning_msg = log.call_args_list[0][0][0]
        assert "uv not found" in warning_msg
        assert "skipping" in warning_msg

        assert files_to_commit == []

    def test_sync_failure_warns_and_continues(self, tmp_path):
        """When sync command fails, logs a warning and continues."""
        (tmp_path / "uv.lock").write_text("content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("uv.lock", 100, 100)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect(
                      sync_result=subprocess.CalledProcessError(1, "uv lock"))),
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        log.assert_called()
        warning_msg = log.call_args_list[0][0][0]
        assert "sync failed" in warning_msg

        assert files_to_commit == []

    def test_sync_timeout_warns_and_continues(self, tmp_path):
        """When sync command times out, logs a warning and continues."""
        (tmp_path / "uv.lock").write_text("content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("uv.lock", 100, 100)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect(
                      sync_result=subprocess.TimeoutExpired("uv lock", 30))),
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        log.assert_called()
        warning_msg = log.call_args_list[0][0][0]
        assert "sync failed" in warning_msg

        assert files_to_commit == []

    def test_unmodified_lockfile_not_added(self, tmp_path):
        """When sync runs but lockfile mtime is unchanged, nothing is added."""
        (tmp_path / "uv.lock").write_text("content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        # Same mtime before and after -- lockfile was not modified
        fake_stat = _make_stat_interceptor("uv.lock", 1000, 1000)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        # Only the sync command should be called (no check-ignore since mtime unchanged)
        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 1
        assert files_to_commit == []

    def test_no_lockfile_is_noop(self, tmp_path):
        """When no lockfiles exist, nothing happens."""
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        with (
            patch("rlsbl.commands.release.shutil.which") as mock_which,
            patch("rlsbl.commands.release.subprocess.run") as mock_run,
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        mock_which.assert_not_called()
        mock_run.assert_not_called()
        assert files_to_commit == []

    def test_multiple_targets_multiple_lockfiles(self, tmp_path):
        """When multiple targets have lockfiles, all are synced."""
        pypi_dir = tmp_path / "pypi"
        pypi_dir.mkdir()
        (pypi_dir / "uv.lock").write_text("content\n")

        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "package-lock.json").write_text("{}\n")

        target_paths = {"pypi": str(pypi_dir), "npm": str(npm_dir)}
        files_to_commit = []
        log = MagicMock()

        # Use full paths to intercept stat only for files that actually exist.
        # os.path.exists also calls os.stat (consuming one call per lockfile),
        # so each real lockfile gets 3 stat calls: exists + before + after.
        known_lockfiles = {
            os.path.join(str(pypi_dir), "uv.lock"): (100, 200),
            os.path.join(str(npm_dir), "package-lock.json"): (300, 400),
        }
        call_counts = {}

        def fake_stat(path, *args, **kwargs):
            spath = str(path)
            if spath in known_lockfiles:
                before, after = known_lockfiles[spath]
                call_counts.setdefault(spath, 0)
                call_counts[spath] += 1
                mock_result = MagicMock()
                mock_result.st_mtime_ns = before if call_counts[spath] <= 2 else after
                return mock_result
            return _real_stat(path, *args, **kwargs)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/tool"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        # 2 sync commands + 2 git check-ignore calls
        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 2
        assert len(files_to_commit) == 2

    def test_duplicate_path_not_added_twice(self, tmp_path):
        """If lockfile path is already in files_to_commit, it is not duplicated."""
        (tmp_path / "uv.lock").write_text("content\n")
        norm_path = os.path.normpath(os.path.join(str(tmp_path), "uv.lock"))
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = [norm_path]
        log = MagicMock()

        fake_stat = _make_stat_interceptor("uv.lock", 100, 200)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect()) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        assert files_to_commit.count(norm_path) == 1

    def test_gitignored_lockfile_skipped(self, tmp_path):
        """When lockfile is gitignored, it is synced but not added to files_to_commit."""
        (tmp_path / "uv.lock").write_text("content\n")
        target_paths = {"pypi": str(tmp_path)}
        files_to_commit = []
        log = MagicMock()

        fake_stat = _make_stat_interceptor("uv.lock", 100, 200)

        with (
            patch("rlsbl.commands.release.shutil.which", return_value="/usr/bin/uv"),
            patch("rlsbl.commands.release.subprocess.run",
                  side_effect=_make_subprocess_run_side_effect(gitignored=True)) as mock_run,
            patch("rlsbl.commands.release.os.stat", side_effect=fake_stat),
        ):
            _sync_lockfiles(target_paths, files_to_commit, log)

        # Sync command should still run (lockfile is updated locally)
        sync_calls = [c for c in mock_run.call_args_list if c[0][0][0] != "git"]
        assert len(sync_calls) == 1

        # But lockfile should NOT be in files_to_commit
        assert files_to_commit == []

        # Warning should be logged
        log_messages = [call[0][0] for call in log.call_args_list]
        assert any("gitignored" in msg for msg in log_messages)
