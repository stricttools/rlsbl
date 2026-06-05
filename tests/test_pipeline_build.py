"""Tests for standalone build_assets functions in rlsbl.pipelines.build."""

import glob
import json
import os
import subprocess
import textwrap

import pytest

from rlsbl.pipelines.build import (
    build_npm_assets,
    build_pypi_assets,
    build_go_assets,
    build_cargo_assets,
    _read_cargo_name,
)
from rlsbl.pipelines.npm import NpmPipeline
from rlsbl.pipelines.pypi import PypiPipeline
from rlsbl.pipelines.go import GoPipeline
from rlsbl.pipelines.cargo import CargoPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_npm_project(tmp_path):
    proj = tmp_path / "npmproj"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({
        "name": "my-npm-pkg",
        "version": "1.0.0",
    }))
    return proj


def _make_pypi_project(tmp_path):
    proj = tmp_path / "pypiproj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "mypkg"
        version = "1.0.0"
    """))
    return proj


def _make_go_project(tmp_path):
    proj = tmp_path / "goproject"
    proj.mkdir()
    (proj / "go.mod").write_text("module example.com/myapp\n\ngo 1.21\n")
    (proj / "main.go").write_text("package main\n\nfunc main() {}\n")
    return proj


def _make_cargo_project(tmp_path):
    proj = tmp_path / "cargoproject"
    proj.mkdir()
    (proj / "Cargo.toml").write_text(textwrap.dedent("""\
        [package]
        name = "mycrate"
        version = "1.0.0"
        edition = "2021"
    """))
    src = proj / "src"
    src.mkdir()
    (src / "main.rs").write_text('fn main() { println!("hello"); }\n')
    return proj


# ---------------------------------------------------------------------------
# build_npm_assets
# ---------------------------------------------------------------------------


class TestBuildNpmAssets:
    def test_calls_npm_pack(self, tmp_path, monkeypatch):
        proj = _make_npm_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "my-npm-pkg-1.0.0.tgz"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        result = build_npm_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "npm"
        assert "pack" in args
        assert "--pack-destination" in args
        assert dist in args
        assert cwd == str(proj)
        assert len(result) == 1
        assert result[0].endswith(".tgz")

    def test_creates_dist_dir(self, tmp_path, monkeypatch):
        proj = _make_npm_project(tmp_path)
        dist = str(tmp_path / "nonexistent" / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        build_npm_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)

    def test_returns_sorted(self, tmp_path, monkeypatch):
        proj = _make_npm_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "z-pkg-1.0.0.tgz"), "w").close()
            open(os.path.join(dist, "a-pkg-1.0.0.tgz"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        result = build_npm_assets(str(proj), "1.0.0", dist)
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# build_pypi_assets
# ---------------------------------------------------------------------------


class TestBuildPypiAssets:
    def test_calls_uv_build(self, tmp_path, monkeypatch):
        proj = _make_pypi_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "mypkg-1.0.0.tar.gz"), "w").close()
            open(os.path.join(dist, "mypkg-1.0.0-py3-none-any.whl"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        result = build_pypi_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "uv"
        assert "build" in args
        assert "--out-dir" in args
        assert dist in args
        assert cwd == str(proj)
        assert len(result) == 2
        assert any(f.endswith(".tar.gz") for f in result)
        assert any(f.endswith(".whl") for f in result)

    def test_creates_dist_dir(self, tmp_path, monkeypatch):
        proj = _make_pypi_project(tmp_path)
        dist = str(tmp_path / "nonexistent" / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        build_pypi_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)


# ---------------------------------------------------------------------------
# build_go_assets
# ---------------------------------------------------------------------------


class TestBuildGoAssets:
    def test_uses_goreleaser_when_available(self, tmp_path, monkeypatch):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            gr_dist = os.path.join(str(proj), "dist")
            for platform in ("myapp_linux_amd64", "myapp_darwin_arm64"):
                d = os.path.join(gr_dist, platform)
                os.makedirs(d, exist_ok=True)
                open(os.path.join(d, "myapp"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        monkeypatch.setattr("rlsbl.pipelines.build.shutil.which", lambda name: "/usr/bin/goreleaser")

        result = build_go_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "goreleaser"
        assert args == ["build", "--snapshot", "--clean"]
        assert cwd == str(proj)
        assert len(result) == 2
        basenames = sorted(os.path.basename(p) for p in result)
        assert basenames == ["myapp_darwin_arm64__myapp", "myapp_linux_amd64__myapp"]

    def test_falls_back_to_go_build(self, tmp_path, monkeypatch, capsys):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "myapp"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        monkeypatch.setattr("rlsbl.pipelines.build.shutil.which", lambda name: None)

        result = build_go_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        assert calls[0][0] == "go"
        assert len(result) == 1
        assert result[0].endswith("myapp")
        assert "goreleaser not found" in capsys.readouterr().out

    def test_falls_back_on_goreleaser_failure(self, tmp_path, monkeypatch, capsys):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            if cmd == "goreleaser":
                raise subprocess.CalledProcessError(1, "goreleaser")
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "myapp"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        monkeypatch.setattr("rlsbl.pipelines.build.shutil.which", lambda name: "/usr/bin/goreleaser")

        result = build_go_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 2
        assert calls[0][0] == "goreleaser"
        assert calls[1][0] == "go"
        assert len(result) == 1
        captured = capsys.readouterr()
        assert "goreleaser failed" in captured.out

    def test_creates_dist_dir(self, tmp_path, monkeypatch):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "nonexistent" / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        monkeypatch.setattr("rlsbl.pipelines.build.shutil.which", lambda name: None)
        build_go_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)


# ---------------------------------------------------------------------------
# build_cargo_assets
# ---------------------------------------------------------------------------


class TestBuildCargoAssets:
    def test_calls_cargo_build_release(self, tmp_path, monkeypatch):
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "dist")
        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            release_dir = os.path.join(str(proj), "target", "release")
            os.makedirs(release_dir, exist_ok=True)
            open(os.path.join(release_dir, "mycrate"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        result = build_cargo_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "cargo"
        assert args == ["build", "--release"]
        assert cwd == str(proj)
        assert len(result) == 1
        assert os.path.basename(result[0]) == "mycrate"

    def test_creates_dist_dir(self, tmp_path, monkeypatch):
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "nonexistent" / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        build_cargo_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)

    def test_no_binary_returns_empty(self, tmp_path, monkeypatch):
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        result = build_cargo_assets(str(proj), "1.0.0", dist)
        assert result == []

    def test_read_cargo_name(self, tmp_path):
        proj = _make_cargo_project(tmp_path)
        assert _read_cargo_name(str(proj)) == "mycrate"


# ---------------------------------------------------------------------------
# Pipeline type delegation
# ---------------------------------------------------------------------------


class TestPipelineBuildDelegation:
    """Verify that pipeline types delegate to the standalone build functions."""

    def test_npm_pipeline_delegates(self, tmp_path, monkeypatch):
        proj = _make_npm_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "pkg-1.0.0.tgz"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        pipeline = NpmPipeline(name="npm", pipeline_type="npm", local=True, config={})
        result = pipeline.build_assets(str(proj), "1.0.0", dist, ctx=None)
        assert len(result) == 1
        assert result[0].endswith(".tgz")

    def test_pypi_pipeline_delegates(self, tmp_path, monkeypatch):
        proj = _make_pypi_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "mypkg-1.0.0.tar.gz"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        pipeline = PypiPipeline(name="pypi", pipeline_type="pypi", local=True, config={})
        result = pipeline.build_assets(str(proj), "1.0.0", dist, ctx=None)
        assert len(result) == 1

    def test_go_pipeline_delegates(self, tmp_path, monkeypatch):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "myapp"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)
        monkeypatch.setattr("rlsbl.pipelines.build.shutil.which", lambda name: None)

        pipeline = GoPipeline(name="go", pipeline_type="go", local=True, config={})
        result = pipeline.build_assets(str(proj), "1.0.0", dist, ctx=None)
        assert len(result) == 1

    def test_cargo_pipeline_delegates(self, tmp_path, monkeypatch):
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "dist")

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            release_dir = os.path.join(str(proj), "target", "release")
            os.makedirs(release_dir, exist_ok=True)
            open(os.path.join(release_dir, "mycrate"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.pipelines.build.run", fake_run)

        pipeline = CargoPipeline(name="cargo", pipeline_type="cargo", local=True, config={})
        result = pipeline.build_assets(str(proj), "1.0.0", dist, ctx=None)
        assert len(result) == 1
        assert os.path.basename(result[0]) == "mycrate"
