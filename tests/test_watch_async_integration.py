"""Integration test for the detached watcher: a real child process.

Spawns an actual `python -m rlsbl watch <sha> --as-daemon-child` via
spawn_detached_watcher against a temp git repo, with a fake `gh` (and
`notify-send`) on PATH simulating one completed passing CI run. Verifies:

- the child runs in its own session (detachment)
- pidfile lifecycle: written by the spawner, removed by the child on exit
- the log file captures the watch output (found runs, pass, audit)
- the child's notification is fire-and-forget (no --action button)
"""

import os
import stat
import subprocess
import sys
import time

import pytest

from rlsbl.commands.watch import _pid_alive, spawn_detached_watcher


FAKE_GH = """#!/bin/bash
case "$1 $2" in
  "repo view")
    echo '{"nameWithOwner":"o/r","name":"r"}'
    ;;
  "run list")
    echo '[{"databaseId":101,"name":"CI","status":"completed","headBranch":"main","workflowName":"CI"}]'
    ;;
  "run watch")
    exit 0
    ;;
  *)
    echo ''
    ;;
esac
"""

FAKE_NOTIFY_SEND = """#!/bin/bash
echo "$@" >> "$NOTIFY_LOG"
"""


def _write_script(path, content):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=str(repo), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def detach_repo(tmp_project, monkeypatch):
    """Temp git repo with one commit, fake gh/notify-send on PATH."""
    _git(tmp_project, "init", "-q", "-b", "main")
    _git(tmp_project, "config", "user.email", "test@test.local")
    _git(tmp_project, "config", "user.name", "Test")
    (tmp_project / "file.txt").write_text("content\n")
    _git(tmp_project, "add", "file.txt")
    _git(tmp_project, "commit", "-q", "-m", "initial")

    fake_bin = tmp_project / "fake-bin"
    fake_bin.mkdir()
    _write_script(fake_bin / "gh", FAKE_GH)
    _write_script(fake_bin / "notify-send", FAKE_NOTIFY_SEND)

    notify_log = tmp_project / "notify.log"
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NOTIFY_LOG", str(notify_log))

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_project),
                         capture_output=True, text=True, check=True).stdout.strip()
    return tmp_project, sha, notify_log


def _wait_for(predicate, timeout=60, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestDetachedWatcherIntegration:

    def test_full_detached_lifecycle(self, detach_repo):
        repo, sha, notify_log = detach_repo

        info = spawn_detached_watcher(sha)
        pid = info["pid"]

        try:
            # Pidfile written by the spawner, recording the child PID
            assert os.path.exists(info["pidfile"])
            with open(info["pidfile"]) as f:
                assert f.read().strip() == str(pid)

            # Detachment: the child is its own session leader, in a
            # different session than this test process
            assert os.getsid(pid) == pid
            assert os.getsid(pid) != os.getsid(0)

            # The child exits on its own (fake CI passes; late re-poll adds
            # ~5s) and removes its pidfile via atexit. This test process is
            # the child's parent (in production the spawner exits and init
            # reaps), so reap it here or it lingers as a zombie.
            def _child_exited():
                try:
                    done_pid, _status = os.waitpid(pid, os.WNOHANG)
                    return done_pid == pid
                except ChildProcessError:
                    return True  # already reaped

            assert _wait_for(_child_exited), (
                "detached watcher did not exit in time"
            )
            assert _wait_for(lambda: not os.path.exists(info["pidfile"]),
                             timeout=10), (
                "child did not remove its pidfile on exit"
            )

            # Log captured the child's output
            with open(info["logfile"]) as f:
                log = f.read()
            assert "found 1 CI run(s), watching" in log
            assert "[CI] passed" in log
            assert "Workflows:" in log

            # Fire-and-forget notification: sent, but without an action
            # button (nothing blocks waiting for a click)
            assert notify_log.exists(), "child sent no notification"
            notify_args = notify_log.read_text()
            assert "CI passed" in notify_args
            assert "--action" not in notify_args
        finally:
            # Never leak the child on failure
            if _pid_alive(pid):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
