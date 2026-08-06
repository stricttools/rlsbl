"""Unit tests for the effect chokepoint's own contracts.

``tests/test_effects_chokepoint.py`` proves that nothing bypasses this module.
These tests prove the module itself behaves as the ~370 converted call sites
expect -- above all the permission model of :func:`effects.atomic_write_text`,
which absorbed three different hand-rolled atomic-write shapes with three
different resulting file modes.
"""

import itertools
import os
import stat
import subprocess
from unittest.mock import patch

import pytest
import strictcli

from rlsbl import effects


_probe_seq = itertools.count(1)


def run_in_preview(fn):
    """Run *fn* inside a real ``--dry-run`` dispatch; return (value, result).

    Preview mode needs a live strictcli effects handle, and only a real
    dispatch mints one -- conftest's ``cli_ctx`` deliberately raises on
    ``.effects`` so nobody fakes it.  Driving a seam through a whole rlsbl
    command just to reach preview mode drags that command's preconditions
    along, so this registers a throwaway one-command app instead: the handle
    and the recording are the framework's real ones, and *fn* is the only
    thing under test.

    ``value`` is whatever *fn* returned; ``result`` is the strictcli test
    result, whose ``stdout`` carries the rendered would-do log.
    """
    box = {}
    app = strictcli.App(
        name=f"previewprobe{next(_probe_seq)}",
        version="0.0.0",
        help="Throwaway app that mints a real effects handle for seam tests.",
    )

    @app.command(
        name="probe",
        help="Run the callable under test inside a real dry-run dispatch.",
        effect="mutating",
    )
    @effects.handler
    def _probe(ctx):
        box["value"] = fn()

    result = app.test(["--dry-run", "probe"])
    return box.get("value"), result


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "f.txt"
        effects.atomic_write_text(str(target), "hello\n")
        assert target.read_text() == "hello\n"

    def test_overwrites_existing_content(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("old")
        effects.atomic_write_text(str(target), "new")
        assert target.read_text() == "new"

    def test_leaves_no_temp_residue_on_success(self, tmp_path):
        target = tmp_path / "f.txt"
        effects.atomic_write_text(str(target), "x")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]

    def test_failure_preserves_original_and_cleans_up(self, tmp_path):
        """A crash at the rename leaves the original intact and no residue."""
        target = tmp_path / "f.txt"
        target.write_text("original")
        with patch("rlsbl.effects.os.replace", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                effects.atomic_write_text(str(target), "replacement")
        assert target.read_text() == "original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]

    def test_new_file_gets_the_umask_default_not_mkstemp_0600(self, tmp_path):
        """The temp file's private 0o600 must never leak onto the target.

        This is the whole reason the helper sets the mode explicitly: the
        shapes it replaced used ``open(path, "w")``, whose result is
        umask-derived.
        """
        target = tmp_path / "f.txt"
        effects.atomic_write_text(str(target), "x")
        current_umask = os.umask(0)
        os.umask(current_umask)
        assert _mode(str(target)) == 0o666 & ~current_umask

    def test_default_relaxes_a_locked_target(self, tmp_path):
        """Without preserve_mode, an existing 0o444 file becomes writable.

        Matches ``open(path, "w")`` on a directory the process can write, which
        is what the plain hand-rolled shape did.
        """
        target = tmp_path / "f.txt"
        target.write_text("x")
        os.chmod(target, 0o444)
        effects.atomic_write_text(str(target), "y")
        assert _mode(str(target)) != 0o444

    def test_preserve_mode_keeps_a_locked_target_locked(self, tmp_path):
        """0o444 released changelog files must not silently become writable."""
        target = tmp_path / "0.1.0.jsonl"
        target.write_text("x")
        os.chmod(target, 0o444)
        effects.atomic_write_text(str(target), "y", preserve_mode=True)
        assert _mode(str(target)) == 0o444
        assert target.read_text() == "y"

    def test_preserve_mode_on_a_new_file_uses_the_umask_default(self, tmp_path):
        target = tmp_path / "new.txt"
        effects.atomic_write_text(str(target), "x", preserve_mode=True)
        current_umask = os.umask(0)
        os.umask(current_umask)
        assert _mode(str(target)) == 0o666 & ~current_umask

    def test_file_mode_is_applied_verbatim(self, tmp_path):
        """The mkstemp-shaped call sites pin their historical 0o600 this way."""
        target = tmp_path / "state.json"
        effects.atomic_write_text(str(target), "{}", file_mode=0o600)
        assert _mode(str(target)) == 0o600

    def test_file_mode_wins_over_an_existing_targets_bits(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("{}")
        os.chmod(target, 0o644)
        effects.atomic_write_text(str(target), "{}", file_mode=0o600)
        assert _mode(str(target)) == 0o600

    def test_file_mode_and_preserve_mode_together_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="not both"):
            effects.atomic_write_text(
                str(tmp_path / "f"), "x", preserve_mode=True, file_mode=0o600
            )

    def test_survives_a_read_only_target_because_rename_is_a_dir_operation(
        self, tmp_path
    ):
        target = tmp_path / "locked.jsonl"
        target.write_text("old")
        os.chmod(target, 0o444)
        effects.atomic_write_text(str(target), "new", preserve_mode=True)
        assert target.read_text() == "new"

    def test_bare_filename_writes_into_the_cwd(self, tmp_path, monkeypatch):
        """os.path.dirname("") must resolve to "." , not the empty string."""
        monkeypatch.chdir(tmp_path)
        effects.atomic_write_text("bare.txt", "x")
        assert (tmp_path / "bare.txt").read_text() == "x"


class TestRun:
    def test_returns_the_completed_process(self):
        result = effects.run(["true"])
        assert result.returncode == 0

    def test_check_raises_on_nonzero(self):
        with pytest.raises(subprocess.CalledProcessError):
            effects.run(["false"], check=True)

    def test_capture_output_and_text(self):
        result = effects.run(["echo", "hi"], capture_output=True, text=True)
        assert result.stdout == "hi\n"

    def test_only_non_default_keywords_reach_subprocess(self):
        """Defaults are omitted so the underlying call matches the direct one.

        Asserted against the primitive module: ``rlsbl.effects.run`` is now a
        router that decides between recording on the effects handle and
        executing here, and it forwards its whole parameter set downwards.
        The kwarg pruning that keeps the executed call byte-identical to the
        direct one it replaced belongs to the primitive.
        """
        from rlsbl import _effects_direct

        with patch("rlsbl._effects_direct.subprocess.run") as mock_run:
            _effects_direct.run(["true"])
            assert mock_run.call_args == ((["true"],), {})

    def test_explicit_keywords_are_forwarded(self, tmp_path):
        from rlsbl import _effects_direct

        with patch("rlsbl._effects_direct.subprocess.run") as mock_run:
            _effects_direct.run(["true"], cwd=str(tmp_path), timeout=5, check=True)
            kwargs = mock_run.call_args[1]
            assert kwargs == {"cwd": str(tmp_path), "timeout": 5, "check": True}


class TestGh:
    def test_prefixes_the_gh_binary(self):
        with patch("rlsbl.effects.run") as mock_run:
            effects.gh(["release", "list"])
            assert mock_run.call_args[0] == (["gh", "release", "list"],)

    def test_injects_gh_repo_without_touching_os_environ(self):
        with patch("rlsbl.effects.run") as mock_run:
            effects.gh(["release", "list"], repo="acme/proj")
            assert mock_run.call_args[1]["env"]["GH_REPO"] == "acme/proj"
        assert "GH_REPO" not in os.environ

    def test_no_env_override_without_a_repo(self):
        with patch("rlsbl.effects.run") as mock_run:
            effects.gh(["auth", "status"])
            assert mock_run.call_args[1]["env"] is None

    def test_an_explicitly_empty_env_stays_empty(self):
        """``env={}`` means an empty child env, never a silent widening."""
        with patch("rlsbl.effects.run") as mock_run:
            effects.gh(["release", "list"], repo="acme/proj", env={})
            assert mock_run.call_args[1]["env"] == {"GH_REPO": "acme/proj"}

    def test_caller_env_is_not_mutated(self):
        caller_env = {"PATH": "/usr/bin"}
        with patch("rlsbl.effects.run"):
            effects.gh(["release", "list"], repo="acme/proj", env=caller_env)
        assert caller_env == {"PATH": "/usr/bin"}

    def test_gh_argv_matches_what_gh_would_execute(self):
        assert effects.gh_argv(["release", "view", "v1"]) == [
            "gh", "release", "view", "v1",
        ]


class TestOpenWrite:
    def test_rejects_a_read_mode(self, tmp_path):
        with pytest.raises(ValueError, match="write mode"):
            effects.open_write(str(tmp_path / "f"), "r")

    def test_accepts_append_and_exclusive_modes(self, tmp_path):
        with effects.open_write(str(tmp_path / "a"), "a", encoding="utf-8") as f:
            f.write("x")
        with effects.open_write(str(tmp_path / "b"), "x", encoding="utf-8") as f:
            f.write("y")
        assert (tmp_path / "a").read_text() == "x"
        assert (tmp_path / "b").read_text() == "y"


class TestSimpleWrappers:
    def test_write_and_append_text(self, tmp_path):
        target = str(tmp_path / "f.txt")
        effects.write_text(target, "a")
        effects.append_text(target, "b")
        assert open(target, encoding="utf-8").read() == "ab"

    def test_write_bytes(self, tmp_path):
        target = str(tmp_path / "f.bin")
        effects.write_bytes(target, b"\x00\x01")
        assert open(target, "rb").read() == b"\x00\x01"

    def test_remove_missing_ok(self, tmp_path):
        effects.remove(str(tmp_path / "absent"), missing_ok=True)
        with pytest.raises(FileNotFoundError):
            effects.remove(str(tmp_path / "absent"))

    def test_makedirs_default_matches_os_makedirs(self, tmp_path):
        d = str(tmp_path / "d")
        effects.makedirs(d)
        with pytest.raises(FileExistsError):
            effects.makedirs(d)
        effects.makedirs(d, exist_ok=True)
