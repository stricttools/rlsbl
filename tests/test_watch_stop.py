"""Tests for `rlsbl watch --stop`: stopping detached watchers.

Covers:
- stopping a live watcher by SHA (SIGTERM, pidfile removed)
- SIGKILL escalation when the process ignores SIGTERM
- stale-pid detection and cleanup
- no-watcher and ambiguous (multiple live) cases
- CLI flag wiring and flag combinations
"""

import os
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

import rlsbl.commands.watch as watch_mod
from rlsbl.commands.watch import _pidfile_path, stop_watcher


SHA = "b" * 40
SHA12 = SHA[:12]


def _setup_state(tmp_project):
    (tmp_project / ".rlsbl").mkdir()


def _spawn_sleeper(reap=False):
    """Start a real child process that sleeps; returns the Popen object.

    With reap=True, a background thread wait()s on the child so it is
    reaped as soon as it dies -- mirroring production, where the detached
    watcher is reparented to init (its spawner has exited) and never
    lingers as a zombie.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if reap:
        threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _write_pidfile(sha, pid):
    path = _pidfile_path(sha)
    with open(path, "w") as f:
        f.write(f"{pid}\n")
    return path


class TestStopBySha:

    def test_stops_live_watcher_and_removes_pidfile(self, tmp_project, capsys):
        _setup_state(tmp_project)
        proc = _spawn_sleeper(reap=True)
        try:
            pidfile = _write_pidfile(SHA, proc.pid)

            with pytest.raises(SystemExit) as exc:
                stop_watcher(SHA12)

            assert exc.value.code == 0
            assert not os.path.exists(pidfile)
            # The child actually died (reap it; wait returns promptly)
            assert proc.wait(timeout=10) != 0
            out = capsys.readouterr().out
            assert "stopped watcher" in out
            assert SHA12 in out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_sigkill_escalation_when_sigterm_ignored(self, tmp_project, capsys):
        _setup_state(tmp_project)
        pidfile = _write_pidfile(SHA, 12345)

        sent = []
        with patch("rlsbl.commands.watch._pid_alive", return_value=True), \
             patch("rlsbl.commands.watch.os.kill", side_effect=lambda p, s: sent.append(s)), \
             patch("rlsbl.commands.watch.time") as mock_time:
            mock_time.time.side_effect = [0, 10, 10, 10]
            mock_time.sleep.return_value = None
            with pytest.raises(SystemExit) as exc:
                stop_watcher(SHA12)

        assert exc.value.code == 0
        import signal
        assert signal.SIGTERM in sent
        assert signal.SIGKILL in sent
        assert not os.path.exists(pidfile)
        assert "did not exit on SIGTERM" in capsys.readouterr().out

    def test_stale_pidfile_reported_and_cleaned(self, tmp_project, capsys):
        _setup_state(tmp_project)
        proc = _spawn_sleeper()
        proc.kill()
        proc.wait()
        pidfile = _write_pidfile(SHA, proc.pid)  # dead pid

        with pytest.raises(SystemExit) as exc:
            stop_watcher(SHA12)

        assert exc.value.code == 0
        assert not os.path.exists(pidfile)
        assert "was not running" in capsys.readouterr().out

    def test_no_pidfile_for_sha_errors(self, tmp_project, capsys):
        _setup_state(tmp_project)

        with pytest.raises(SystemExit) as exc:
            stop_watcher(SHA12)

        assert exc.value.code == 1
        assert "no detached watcher found" in capsys.readouterr().err


class TestStopWithoutSha:

    def test_no_watchers_errors(self, tmp_project, capsys):
        _setup_state(tmp_project)

        with pytest.raises(SystemExit) as exc:
            stop_watcher(None)

        assert exc.value.code == 1
        assert "no live detached watchers" in capsys.readouterr().err

    def test_single_live_watcher_is_stopped(self, tmp_project, capsys):
        _setup_state(tmp_project)
        proc = _spawn_sleeper(reap=True)
        try:
            pidfile = _write_pidfile(SHA, proc.pid)

            with pytest.raises(SystemExit) as exc:
                stop_watcher(None)

            assert exc.value.code == 0
            assert not os.path.exists(pidfile)
            assert proc.wait(timeout=10) != 0
            assert "stopped watcher" in capsys.readouterr().out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stale_pidfiles_cleaned_before_deciding(self, tmp_project, capsys):
        """A stale pidfile does not count as a live watcher: it is removed
        and, with nothing live left, --stop errors."""
        _setup_state(tmp_project)
        proc = _spawn_sleeper()
        proc.kill()
        proc.wait()
        pidfile = _write_pidfile(SHA, proc.pid)

        with pytest.raises(SystemExit) as exc:
            stop_watcher(None)

        assert exc.value.code == 1
        assert not os.path.exists(pidfile)
        captured = capsys.readouterr()
        assert "stale" in captured.err
        assert "no live detached watchers" in captured.err

    def test_multiple_live_watchers_lists_and_errors(self, tmp_project, capsys):
        _setup_state(tmp_project)
        proc_a = _spawn_sleeper()
        proc_b = _spawn_sleeper()
        try:
            sha_a = "c" * 40
            sha_b = "d" * 40
            _write_pidfile(sha_a, proc_a.pid)
            _write_pidfile(sha_b, proc_b.pid)

            with pytest.raises(SystemExit) as exc:
                stop_watcher(None)

            assert exc.value.code == 1
            err = capsys.readouterr().err
            assert "multiple live detached watchers" in err
            assert f"rlsbl watch --stop {sha_a[:12]}" in err
            assert f"rlsbl watch --stop {sha_b[:12]}" in err
            # Both are still running -- nothing was killed
            assert proc_a.poll() is None
            assert proc_b.poll() is None
        finally:
            for p in (proc_a, proc_b):
                if p.poll() is None:
                    p.kill()
                    p.wait()


class TestStopCliWiring:

    def test_stop_flag_routes_to_stop_watcher(self):
        from rlsbl import app

        with patch("rlsbl.commands.watch.run_cmd") as mock_cmd:
            result = app.test(["watch", "--stop", SHA12])

        assert result.exit_code == 0, result.stderr
        args, flags = mock_cmd.call_args[0][1], mock_cmd.call_args[0][2]
        assert args == [SHA12]
        assert flags.get("stop") is True

    def test_stop_without_sha_parses(self):
        from rlsbl import app

        with patch("rlsbl.commands.watch.run_cmd") as mock_cmd:
            result = app.test(["watch", "--stop"])

        assert result.exit_code == 0, result.stderr
        args, flags = mock_cmd.call_args[0][1], mock_cmd.call_args[0][2]
        assert args == []
        assert flags.get("stop") is True

    def test_stop_with_run_id_errors(self):
        from rlsbl import app

        result = app.test(["watch", "--stop", "--run-id", "123"])
        assert result.exit_code == 1
        assert "--stop" in result.stderr and "--run-id" in result.stderr

    def test_stop_with_as_daemon_child_errors(self):
        from rlsbl import app

        result = app.test(["watch", "--stop", "--as-daemon-child"])
        assert result.exit_code == 1

    def test_run_cmd_dispatches_stop_before_gh_lookups(self, tmp_project):
        """The stop branch must run before any gh/repo resolution."""
        _setup_state(tmp_project)
        with patch("rlsbl.commands.watch.stop_watcher",
                   side_effect=SystemExit(0)) as mock_stop, \
             patch("rlsbl.commands.watch.run_gh") as mock_gh:
            with pytest.raises(SystemExit):
                watch_mod.run_cmd(None, [SHA12], {"stop": True})
        mock_stop.assert_called_once_with(SHA12)
        mock_gh.assert_not_called()
