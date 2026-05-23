"""Tests for build_assets() across all target implementations."""

import glob
import json
import os
import textwrap

import pytest

from rlsbl.targets.base import BaseTarget
from rlsbl.targets.pypi import PypiTarget
from rlsbl.targets.npm import NpmTarget
from rlsbl.targets.go import GoTarget
from rlsbl.targets.cargo import CargoTarget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pypi_project(tmp_path):
    """Create a minimal pyproject.toml project."""
    proj = tmp_path / "pyproject"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "mypkg"
        version = "1.0.0"
    """))
    return proj


def _make_npm_project(tmp_path):
    """Create a minimal package.json project."""
    proj = tmp_path / "npmproject"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({
        "name": "my-npm-pkg",
        "version": "1.0.0",
    }))
    return proj


def _make_go_project(tmp_path):
    """Create a minimal Go project with go.mod and main.go."""
    proj = tmp_path / "goproject"
    proj.mkdir()
    (proj / "go.mod").write_text("module example.com/myapp\n\ngo 1.21\n")
    (proj / "main.go").write_text("package main\n\nfunc main() {}\n")
    return proj


def _make_cargo_project(tmp_path):
    """Create a minimal Cargo project."""
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
# BaseTarget raises NotImplementedError
# ---------------------------------------------------------------------------


class ConcreteBase(BaseTarget):
    """Minimal concrete subclass so we can instantiate BaseTarget."""

    @property
    def name(self):
        return "test"

    def detect(self, dir_path):
        return True

    def read_version(self, dir_path):
        return "0.0.0"


class TestBaseTarget:
    def test_build_assets_raises(self, tmp_path):
        target = ConcreteBase()
        dist = str(tmp_path / "dist")
        with pytest.raises(NotImplementedError, match="Asset builds not supported for target 'test'"):
            target.build_assets(str(tmp_path), "1.0.0", dist)


# ---------------------------------------------------------------------------
# PyPI target
# ---------------------------------------------------------------------------


class TestPypiTarget:
    def test_calls_uv_build(self, tmp_path, monkeypatch):
        proj = _make_pypi_project(tmp_path)
        dist = str(tmp_path / "dist")
        target = PypiTarget()

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            # Simulate creating a file in dist_dir
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "mypkg-1.0.0.tar.gz"), "w").close()
            open(os.path.join(dist, "mypkg-1.0.0-py3-none-any.whl"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.targets.pypi.run", fake_run)

        result = target.build_assets(str(proj), "1.0.0", dist)

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
        target = PypiTarget()

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.targets.pypi.run", fake_run)

        target.build_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)


# ---------------------------------------------------------------------------
# npm target
# ---------------------------------------------------------------------------


class TestNpmTarget:
    def test_calls_npm_pack(self, tmp_path, monkeypatch):
        proj = _make_npm_project(tmp_path)
        dist = str(tmp_path / "dist")
        target = NpmTarget()

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            # Simulate creating a tarball in dist_dir
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "my-npm-pkg-1.0.0.tgz"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.targets.npm.run", fake_run)

        result = target.build_assets(str(proj), "1.0.0", dist)

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
        target = NpmTarget()

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.targets.npm.run", fake_run)

        target.build_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)


# ---------------------------------------------------------------------------
# Go target
# ---------------------------------------------------------------------------


class TestGoTarget:
    def test_calls_go_build(self, tmp_path, monkeypatch):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "dist")
        target = GoTarget()

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            # Simulate creating a binary in dist_dir
            os.makedirs(dist, exist_ok=True)
            open(os.path.join(dist, "myapp"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.targets.go.run", fake_run)

        result = target.build_assets(str(proj), "1.0.0", dist)

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "go"
        assert "build" in args
        assert "-o" in args
        assert "./..." in args
        assert cwd == str(proj)

        assert len(result) == 1
        assert result[0].endswith("myapp")

    def test_creates_dist_dir(self, tmp_path, monkeypatch):
        proj = _make_go_project(tmp_path)
        dist = str(tmp_path / "nonexistent" / "dist")
        target = GoTarget()

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.targets.go.run", fake_run)

        target.build_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)


# ---------------------------------------------------------------------------
# Cargo target
# ---------------------------------------------------------------------------


class TestCargoTarget:
    def test_calls_cargo_build_release(self, tmp_path, monkeypatch):
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "dist")
        target = CargoTarget()

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            # Simulate creating the release binary
            release_dir = os.path.join(str(proj), "target", "release")
            os.makedirs(release_dir, exist_ok=True)
            open(os.path.join(release_dir, "mycrate"), "w").close()
            return ""

        monkeypatch.setattr("rlsbl.targets.cargo.run", fake_run)

        result = target.build_assets(str(proj), "1.0.0", dist)

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
        target = CargoTarget()

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            return ""

        monkeypatch.setattr("rlsbl.targets.cargo.run", fake_run)

        target.build_assets(str(proj), "1.0.0", dist)
        assert os.path.isdir(dist)

    def test_no_binary_returns_empty(self, tmp_path, monkeypatch):
        """When the release binary doesn't exist, returns empty list."""
        proj = _make_cargo_project(tmp_path)
        dist = str(tmp_path / "dist")
        target = CargoTarget()

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            # Don't create any binary -- simulates a library crate
            return ""

        monkeypatch.setattr("rlsbl.targets.cargo.run", fake_run)

        result = target.build_assets(str(proj), "1.0.0", dist)
        assert result == []
