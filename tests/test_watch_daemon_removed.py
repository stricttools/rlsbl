"""Regression tests for the deletion of the detached watch daemon.

The detached-watcher machinery (``--watch-async``, ``rlsbl watch --stop``,
the internal ``--as-daemon-child`` child mode, and the pidfile/logfile
lifecycle under ``.rlsbl/``) was removed: releases either watch CI in
process or print the manual ``rlsbl watch <sha>`` hint.

These tests pin the removal so the machinery cannot silently return, and
pin the ONE piece that deliberately survived: :func:`poll_runs`, which the
in-process CI wait reuses.
"""

import inspect
import re
from pathlib import Path

import pytest

import rlsbl
from rlsbl.commands import watch as watch_mod
from rlsbl.commands.release.shared import build_release_flags

app = rlsbl.app

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "rlsbl"

pytestmark = pytest.mark.repo_cwd


# ---------------------------------------------------------------------------
# Flag surface: the daemon flags no longer parse
# ---------------------------------------------------------------------------


class TestDaemonFlagsRejected:
    """Every removed flag must be an unknown-flag parse error."""

    @pytest.mark.parametrize("argv", [
        ["release", "run", "--bump", "patch", "--description", "d",
         "--no-allow-dirty", "--watch-async"],
        ["release", "run", "--bump", "patch", "--description", "d",
         "--no-allow-dirty", "--no-watch-async"],
        ["release", "resume", "--watch-async"],
        ["release", "retry", "--watch-async"],
        ["monorepo", "release", "run", "--no-allow-dirty", "--watch-async"],
        ["watch", "--stop"],
        ["watch", "--as-daemon-child"],
    ])
    def test_removed_flag_is_unknown(self, argv):
        result = app.test(argv)
        assert result.exit_code != 0
        assert "unknown flag" in result.stderr

    @pytest.mark.parametrize("argv", [
        ["release", "run"],
        ["release", "resume"],
        ["release", "retry"],
        ["monorepo", "release", "run"],
    ])
    def test_watch_survives_as_plain_flag(self, argv):
        """--watch/--no-watch survive as a plain negatable bool, sans async twin."""
        result = app.test(argv + ["--help"])
        assert result.exit_code == 0, result.stderr
        assert "--watch" in result.stdout
        assert "watch-async" not in result.stdout

    def test_watch_command_help_has_no_daemon_flags(self):
        result = app.test(["watch", "--help"])
        assert result.exit_code == 0, result.stderr
        assert "--stop" not in result.stdout
        assert "as-daemon-child" not in result.stdout


# ---------------------------------------------------------------------------
# Module surface: the daemon helpers are gone, poll_runs survives
# ---------------------------------------------------------------------------


class TestWatchModuleSurface:

    @pytest.mark.parametrize("name", [
        "spawn_detached_watcher",
        "stop_watcher",
        "_stop_one",
        "_pidfile_path",
        "_logfile_path",
        "_watch_state_dir",
        "_read_pidfile",
        "_cleanup_own_pidfiles",
        "_sha12_from_pidfile",
        "_pid_alive",
        "_pgid_alive",
        "_fire_and_forget_notify",
    ])
    def test_daemon_helper_removed(self, name):
        assert not hasattr(watch_mod, name), (
            f"{name} is detached-watcher machinery and must stay deleted"
        )

    def test_poll_runs_preserved(self):
        """HARD CONSTRAINT: poll_runs is reused by the in-process CI wait."""
        assert callable(watch_mod.poll_runs)
        params = inspect.signature(watch_mod.poll_runs).parameters
        assert list(params) == ["commit_sha", "max_attempts", "interval"]

    def test_build_release_flags_has_no_watch_async(self):
        params = inspect.signature(build_release_flags).parameters
        assert "watch_async" not in params
        flags = build_release_flags(False, False, False, False, watch=True)
        assert "watch-async" not in flags
        assert flags["watch"] is True


# ---------------------------------------------------------------------------
# Source sweep: no production code still mentions the daemon
# ---------------------------------------------------------------------------


_FORBIDDEN = re.compile(
    r"watch-async|watch_async|as-daemon-child|as_daemon_child|"
    r"pidfile|spawn_detached_watcher|stop_watcher",
    re.IGNORECASE,
)


def test_no_daemon_references_in_production_code():
    offenders = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "detached-watcher references remain:\n" + "\n".join(offenders)
