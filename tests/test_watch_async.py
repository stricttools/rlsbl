"""Tests for the detached watcher machinery (--watch-async support in watch.py).

Covers:
- _watch_state_dir resolution (repo root .rlsbl / .rlsbl-monorepo / fallback)
- spawn_detached_watcher: pidfile lifecycle, child command line, detachment
  options, live-watcher refusal, stale-pid cleanup (Popen mocked)
- fire-and-forget notifications in daemon-child mode
- _cleanup_own_pidfiles removes only this process's pidfiles
- CLI wiring of the internal --as-daemon-child flag
"""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import rlsbl.commands.watch as watch_mod
from rlsbl.commands.watch import (
    _cleanup_own_pidfiles,
    _logfile_path,
    _pid_alive,
    _pidfile_path,
    _watch_state_dir,
    spawn_detached_watcher,
)


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)


SHA = "a" * 40
SHA12 = SHA[:12]


@pytest.fixture(autouse=True)
def _reset_fire_and_forget():
    """Keep the module-level notify mode from leaking across tests."""
    watch_mod._fire_and_forget_notify = False
    yield
    watch_mod._fire_and_forget_notify = False


# ---------------------------------------------------------------------------
# _watch_state_dir
# ---------------------------------------------------------------------------


class TestWatchStateDir:

    def test_uses_repo_root_rlsbl_dir(self, tmp_project, monkeypatch):
        _git_init(tmp_project)
        (tmp_project / ".rlsbl").mkdir()
        sub = tmp_project / "packages" / "core"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert _watch_state_dir() == str(tmp_project / ".rlsbl")

    def test_falls_back_to_monorepo_dir(self, tmp_project):
        _git_init(tmp_project)
        (tmp_project / ".rlsbl-monorepo").mkdir()
        assert _watch_state_dir() == str(tmp_project / ".rlsbl-monorepo")

    def test_prefers_rlsbl_over_monorepo(self, tmp_project):
        _git_init(tmp_project)
        (tmp_project / ".rlsbl").mkdir()
        (tmp_project / ".rlsbl-monorepo").mkdir()
        assert _watch_state_dir() == str(tmp_project / ".rlsbl")

    def test_creates_rlsbl_dir_when_missing(self, tmp_project):
        _git_init(tmp_project)
        result = _watch_state_dir()
        assert result == str(tmp_project / ".rlsbl")
        assert os.path.isdir(result)

    def test_pidfile_and_logfile_use_local_only_suffix(self, tmp_project):
        """.local-only suffix keeps the files out of git (fleet-wide ignore)."""
        _git_init(tmp_project)
        (tmp_project / ".rlsbl").mkdir()
        assert _pidfile_path(SHA) == str(
            tmp_project / ".rlsbl" / f"watch-{SHA12}.pid.local-only"
        )
        assert _logfile_path(SHA) == str(
            tmp_project / ".rlsbl" / f"watch-{SHA12}.log.local-only"
        )


# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------


class TestPidAlive:

    def test_own_pid_is_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_dead_pid_is_not_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert _pid_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# spawn_detached_watcher
# ---------------------------------------------------------------------------


def _setup_repo(tmp_project):
    _git_init(tmp_project)
    (tmp_project / ".rlsbl").mkdir()


def _mock_sha_run(cmd, args=None, **kwargs):
    """Stand-in for utils.run: resolves rev-parse of the SHA, keeps
    --show-toplevel working against the real cwd repo."""
    if args and args[0] == "rev-parse" and args[1] == "--show-toplevel":
        return os.getcwd()
    return SHA


class TestSpawnDetachedWatcher:

    def test_spawns_child_and_writes_pidfile(self, tmp_project, capsys):
        _setup_repo(tmp_project)

        with patch("rlsbl.commands.watch.run", side_effect=_mock_sha_run), \
             patch("rlsbl.commands.watch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=4242)
            info = spawn_detached_watcher(SHA12)

            assert info["pid"] == 4242
            pidfile = _pidfile_path(SHA)
            assert info["pidfile"] == pidfile
            with open(pidfile) as f:
                assert f.read().strip() == "4242"

            cmd = mock_popen.call_args[0][0]
            assert cmd[:4] == [sys.executable, "-m", "rlsbl", "watch"]
            assert SHA in cmd
            assert "--as-daemon-child" in cmd

            kwargs = mock_popen.call_args[1]
            assert kwargs["start_new_session"] is True
            assert kwargs["stdin"] == subprocess.DEVNULL

        out = capsys.readouterr().out
        assert "detached watcher" in out
        assert info["logfile"] in out
        assert f"rlsbl watch --stop {SHA12}" in out

    def test_run_ids_mode_passes_run_id_flags(self, tmp_project):
        _setup_repo(tmp_project)

        with patch("rlsbl.commands.watch.run", side_effect=_mock_sha_run), \
             patch("rlsbl.commands.watch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=4243)
            spawn_detached_watcher(SHA12, run_ids=["11", "22"])
            cmd = mock_popen.call_args[0][0]

        assert SHA not in cmd  # run-id mode has no sha positional
        assert cmd.count("--run-id") == 2
        assert "11" in cmd and "22" in cmd
        assert "--as-daemon-child" in cmd

    def test_refuses_when_live_watcher_exists(self, tmp_project, capsys):
        _setup_repo(tmp_project)
        with open(_pidfile_path(SHA), "w") as f:
            f.write(f"{os.getpid()}\n")  # our own pid: definitely alive

        with patch("rlsbl.commands.watch.run", side_effect=_mock_sha_run), \
             patch("rlsbl.commands.watch.subprocess.Popen") as mock_popen:
            with pytest.raises(SystemExit) as exc:
                spawn_detached_watcher(SHA12)
            mock_popen.assert_not_called()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "already running" in err
        assert f"rlsbl watch --stop {SHA12}" in err

    def test_cleans_stale_pidfile_and_proceeds(self, tmp_project, capsys):
        _setup_repo(tmp_project)
        with open(_pidfile_path(SHA), "w") as f:
            f.write("99999\n")

        with patch("rlsbl.commands.watch.run", side_effect=_mock_sha_run), \
             patch("rlsbl.commands.watch._pid_alive", return_value=False), \
             patch("rlsbl.commands.watch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=4244)
            info = spawn_detached_watcher(SHA12)

        assert info["pid"] == 4244
        with open(_pidfile_path(SHA)) as f:
            assert f.read().strip() == "4244"
        assert "stale" in capsys.readouterr().err

    def test_unresolvable_sha_used_as_is(self, tmp_project):
        """When git rev-parse fails, the raw sha argument is used."""
        (tmp_project / ".rlsbl").mkdir()

        with patch("rlsbl.commands.watch.run", side_effect=Exception("not a git repo")), \
             patch("rlsbl.commands.watch.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=4245)
            info = spawn_detached_watcher("deadbeef1234")
            cmd = mock_popen.call_args[0][0]

        assert "deadbeef1234" in cmd
        assert info["pidfile"].endswith("watch-deadbeef1234.pid.local-only")


# ---------------------------------------------------------------------------
# Fire-and-forget notifications (daemon-child mode)
# ---------------------------------------------------------------------------


class TestFireAndForgetNotify:

    @patch("rlsbl.commands.watch.require_tool", return_value="/usr/bin/notify-send")
    @patch("rlsbl.commands.watch.subprocess.run")
    def test_interactive_mode_uses_action_and_blocks(self, mock_run, _tool, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        mock_run.return_value = MagicMock(stdout="")
        watch_mod._notify("title", "body", url="https://example.com")
        cmd = mock_run.call_args[0][0]
        assert "--action" in cmd
        assert mock_run.call_args[1]["timeout"] == 120

    @patch("rlsbl.commands.watch.require_tool", return_value="/usr/bin/notify-send")
    @patch("rlsbl.commands.watch.subprocess.run")
    def test_daemon_child_mode_is_fire_and_forget(self, mock_run, _tool, monkeypatch):
        """In daemon-child mode the notification has no action button and
        no long blocking wait for a click."""
        monkeypatch.setattr(sys, "platform", "linux")
        watch_mod._fire_and_forget_notify = True
        mock_run.return_value = MagicMock(stdout="")
        watch_mod._notify("title", "body", url="https://example.com")
        cmd = mock_run.call_args[0][0]
        assert "--action" not in cmd
        assert mock_run.call_args[1]["timeout"] <= 10

    @patch("rlsbl.commands.watch._open_url")
    @patch("rlsbl.commands.watch.require_tool", return_value="/usr/bin/notify-send")
    @patch("rlsbl.commands.watch.subprocess.run")
    def test_daemon_child_mode_never_opens_url(self, mock_run, _tool, mock_open,
                                               monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        watch_mod._fire_and_forget_notify = True
        mock_run.return_value = MagicMock(stdout="open")
        watch_mod._notify("title", "body", url="https://example.com")
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# _cleanup_own_pidfiles
# ---------------------------------------------------------------------------


class TestCleanupOwnPidfiles:

    def test_removes_only_own_pidfiles(self, tmp_project):
        _setup_repo(tmp_project)
        state = tmp_project / ".rlsbl"
        own = state / "watch-aaaaaaaaaaaa.pid.local-only"
        own.write_text(f"{os.getpid()}\n")
        other = state / "watch-bbbbbbbbbbbb.pid.local-only"
        other.write_text("99999\n")

        _cleanup_own_pidfiles()

        assert not own.exists()
        assert other.exists()

    def test_noop_without_state_dir_pidfiles(self, tmp_project):
        _setup_repo(tmp_project)
        _cleanup_own_pidfiles()  # must not raise


# ---------------------------------------------------------------------------
# run_cmd daemon-child wiring
# ---------------------------------------------------------------------------


class TestRunCmdDaemonChild:

    @patch("rlsbl.commands.watch.atexit.register")
    @patch("rlsbl.commands.watch._notify")
    @patch("rlsbl.commands.watch._print_workflow_audit")
    @patch("rlsbl.commands.watch._watch_runs")
    @patch("rlsbl.commands.watch.poll_runs")
    @patch("rlsbl.commands.watch.run_gh")
    @patch("rlsbl.commands.watch.run")
    @patch("rlsbl.commands.watch.time")
    def test_daemon_child_flag_enables_fire_and_forget_and_cleanup(
        self, _time, mock_run, mock_run_gh, mock_poll, mock_watch, _audit,
        _notify, mock_atexit,
    ):
        mock_run.return_value = SHA
        mock_run_gh.return_value = json.dumps({"nameWithOwner": "o/r", "name": "r"})
        mock_poll.side_effect = [
            [{"databaseId": 1, "name": "CI", "status": "completed"}],
            [],
        ]
        mock_watch.return_value = [{"name": "CI", "passed": True, "run_id": "1"}]

        with pytest.raises(SystemExit) as exc:
            watch_mod.run_cmd(None, [SHA12], {"as-daemon-child": True})

        assert exc.value.code == 0
        assert watch_mod._fire_and_forget_notify is True
        mock_atexit.assert_called_once_with(_cleanup_own_pidfiles)


# ---------------------------------------------------------------------------
# CLI wiring: rlsbl watch --as-daemon-child
# ---------------------------------------------------------------------------


class TestCliDaemonChildFlag:

    def test_as_daemon_child_flag_threads_into_flags(self):
        from rlsbl import app

        with patch("rlsbl.commands.watch.run_cmd") as mock_cmd:
            result = app.test(["watch", SHA12, "--as-daemon-child"])

        assert result.exit_code == 0, result.stderr
        args, flags = mock_cmd.call_args[0][1], mock_cmd.call_args[0][2]
        assert args == [SHA12]
        assert flags.get("as-daemon-child") is True

    def test_flag_defaults_to_false(self):
        from rlsbl import app

        with patch("rlsbl.commands.watch.run_cmd") as mock_cmd:
            result = app.test(["watch", SHA12])

        assert result.exit_code == 0, result.stderr
        flags = mock_cmd.call_args[0][2]
        assert flags.get("as-daemon-child") is False
