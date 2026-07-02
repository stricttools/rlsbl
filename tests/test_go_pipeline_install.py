"""Tests for GoPipeline publish hard errors and declared install_paths.

The go pipeline with ``local: true`` must never warn-and-continue:
missing toolchain, unreadable go.mod, proxy notification failure,
missing/invalid install_paths, and install failure are all hard errors.
The outer release flow decides fatality -- the pipeline itself raises.
"""

import subprocess
from unittest.mock import patch

import pytest

from rlsbl.errors import ConfigError
from rlsbl.go_introspect import GoIntrospectError
from rlsbl.pipelines.go import GoPipeline


GO_MOD = "module github.com/user/repo\n\ngo 1.21\n"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _pipeline(install_paths=None, local=True):
    config = {"type": "go", "local": local}
    if install_paths is not None:
        config["install_paths"] = install_paths
    return GoPipeline("go", "go", local, config)


class TestPublishHardErrors:
    def test_local_false_skips(self, capsys):
        p = _pipeline(local=False)
        p.publish("/nonexistent", "1.0.0", None)
        assert "Skipping" in capsys.readouterr().out

    def test_unreadable_go_mod_raises(self, tmp_path):
        p = _pipeline(install_paths=["."])
        with pytest.raises(ConfigError, match="module path"):
            p.publish(str(tmp_path), "1.0.0", None)

    def test_missing_go_tool_raises(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        p = _pipeline(install_paths=["."])
        with patch("rlsbl.pipelines.go.require_tool",
                   side_effect=FileNotFoundError("Required tool not found on PATH: go")):
            with pytest.raises(FileNotFoundError, match="go"):
                p.publish(str(tmp_path), "1.0.0", None)

    def test_proxy_notification_failure_raises(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        p = _pipeline(install_paths=["."])
        with patch("rlsbl.pipelines.go.require_tool", return_value="/usr/bin/go"):
            with patch("rlsbl.pipelines.go.run",
                       side_effect=subprocess.CalledProcessError(1, "go")):
                with pytest.raises(RuntimeError, match="proxy notification failed"):
                    p.publish(str(tmp_path), "1.0.0", None)

    def test_missing_install_paths_is_hard_error_with_detected_mains(self, tmp_path):
        """The error must name install_paths and show what go list detected,
        so migration is a copy-paste."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        p = _pipeline()  # no install_paths declared
        with patch("rlsbl.pipelines.go.require_tool", return_value="/usr/bin/go"):
            with patch("rlsbl.pipelines.go.run"):
                with pytest.raises(ConfigError) as exc:
                    p.publish(str(tmp_path), "1.0.0", None)
        msg = str(exc.value)
        assert "install_paths" in msg
        assert "./cmd/x" in msg
        # The suggestion advertises pasting into .rlsbl/config.json, so it
        # must be valid JSON (double quotes), not a Python list repr.
        assert '["./cmd/x"]' in msg

    def test_declared_path_not_a_main_package_raises(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "internal" / "lib.go", "package lib\n")
        p = _pipeline(install_paths=["./internal"])
        with patch("rlsbl.pipelines.go.require_tool", return_value="/usr/bin/go"):
            with patch("rlsbl.pipelines.go.run"):
                with pytest.raises(GoIntrospectError, match="not a main package"):
                    p.publish(str(tmp_path), "1.0.0", None)

    def test_install_failure_raises(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        p = _pipeline(install_paths=["."])
        with patch("rlsbl.pipelines.go.require_tool", return_value="/usr/bin/go"):
            with patch("rlsbl.pipelines.go.run"):
                # Validation passes (identity); the go install call itself fails.
                with patch("rlsbl.pipelines.go.validate_install_paths",
                           side_effect=lambda d, paths: paths):
                    with patch("rlsbl.pipelines.go.subprocess.run",
                               side_effect=subprocess.CalledProcessError(1, "go")):
                        with pytest.raises(RuntimeError, match="go install failed"):
                            p.publish(str(tmp_path), "1.0.0", None)

    def test_publish_installs_declared_paths(self, tmp_path, capsys):
        """cmd/x/cli.go layout installs via the declared path -- the old
        main.go glob skipped these projects forever."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        p = _pipeline(install_paths=["./cmd/x"])
        installed = []

        def fake_install(cmd, **kwargs):
            installed.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with patch("rlsbl.pipelines.go.require_tool", return_value="/usr/bin/go"):
            with patch("rlsbl.pipelines.go.run"):
                real_run = subprocess.run

                def dispatch(cmd, **kwargs):
                    if cmd[:2] == ["go", "install"]:
                        return fake_install(cmd, **kwargs)
                    return real_run(cmd, **kwargs)

                with patch("rlsbl.pipelines.go.subprocess.run", side_effect=dispatch):
                    p.publish(str(tmp_path), "1.0.0", None)

        assert installed == [["go", "install", "./cmd/x"]]
        assert "Installed" in capsys.readouterr().out
