"""Tests for PypiTarget.build() -- path dep rewriting during build."""

import os
import subprocess
import textwrap

import pytest

from rlsbl.targets.pypi import PypiTarget


@pytest.fixture
def target():
    return PypiTarget()


def _make_standalone_project(tmp_path):
    """Create a standalone project (no monorepo)."""
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "myproject"
        version = "1.0.0"
        dependencies = ["requests>=2.0"]
    """))
    return proj


def _make_workspace(tmp_path):
    """Create a monorepo workspace with app depending on lib via path dep."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mono_dir = workspace / ".rlsbl-monorepo"
    mono_dir.mkdir()
    (mono_dir / "workspace.toml").write_text(textwrap.dedent("""\
        [[projects]]
        path = "packages/app"
        name = "app"

        [[projects]]
        path = "packages/lib"
        name = "lib"
    """))

    app_dir = workspace / "packages" / "app"
    app_dir.mkdir(parents=True)
    app_pkg = app_dir / "app"
    app_pkg.mkdir()
    (app_pkg / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (app_dir / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "app"
        version = "1.0.0"
        dependencies = [
            "lib @ {root:uri}/../lib",
            "requests>=2.0",
        ]
    """))

    lib_dir = workspace / "packages" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "lib"
        version = "0.5.0"
    """))

    return workspace, app_dir, lib_dir


class TestBuildNoMonorepo:
    """build() without monorepo context runs uv build in place."""

    def test_no_workspace_root_runs_uv_build(self, tmp_path, target, monkeypatch):
        """When there is no workspace root, uv build runs in the project dir."""
        proj = _make_standalone_project(tmp_path)

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            return ""

        monkeypatch.setattr("rlsbl.targets.pypi.run", fake_run)

        target.build(str(proj), "1.0.0")

        assert len(calls) == 1
        cmd, args, cwd = calls[0]
        assert cmd == "uv"
        assert args[0] == "build"
        assert "--out-dir" in args
        assert cwd == str(proj)

    def test_no_path_deps_runs_uv_build(self, tmp_path, target, monkeypatch):
        """When workspace exists but project has no path deps, uv build runs in place."""
        workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        # Overwrite app's pyproject to have no path deps
        (app_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "app"
            version = "1.0.0"
            dependencies = ["requests>=2.0"]
        """))

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            return ""

        monkeypatch.setattr("rlsbl.targets.pypi.run", fake_run)

        target.build(str(app_dir), "1.0.0")

        assert len(calls) == 1
        assert calls[0][0] == "uv"
        assert calls[0][1][0] == "build"
        assert calls[0][2] == str(app_dir)


class TestBuildWithPathDeps:
    """build() with monorepo + path deps rewrites in a temp dir."""

    def test_creates_temp_dir_and_rewrites(self, tmp_path, target, monkeypatch):
        """build() copies to temp dir, rewrites deps, runs uv build, cleans up."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        subprocess_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            subprocess_calls.append({"cmd": cmd, "kwargs": kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        target.build(str(app_dir), "1.0.0")

        assert len(subprocess_calls) == 1
        call = subprocess_calls[0]
        assert call["cmd"][:2] == ["uv", "build"]
        assert "--out-dir" in call["cmd"]

        # The cwd should be a temp directory (not the real project)
        cwd = call["kwargs"].get("cwd", "")
        assert cwd != str(app_dir)
        assert "rlsbl-build-" in cwd

        # The --out-dir should point to the real project's dist/
        out_dir_idx = call["cmd"].index("--out-dir")
        out_dir = call["cmd"][out_dir_idx + 1]
        assert out_dir == os.path.join(str(app_dir), "dist")

    def test_temp_dir_cleaned_up(self, tmp_path, target, monkeypatch):
        """The temp build directory is removed after build completes."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        temp_dirs_seen = []

        def fake_subprocess_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            if "rlsbl-build-" in cwd:
                temp_dirs_seen.append(cwd)
                assert os.path.isdir(cwd), "temp dir should exist during build"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        target.build(str(app_dir), "1.0.0")

        assert len(temp_dirs_seen) == 1
        assert not os.path.exists(temp_dirs_seen[0])

    def test_working_tree_pyproject_unchanged(self, tmp_path, target, monkeypatch):
        """The real pyproject.toml is NOT modified during or after build."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        original_pyproject = (app_dir / "pyproject.toml").read_text()

        def fake_subprocess_run(cmd, **kwargs):
            current = (app_dir / "pyproject.toml").read_text()
            assert current == original_pyproject, "pyproject.toml was modified during build"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        target.build(str(app_dir), "1.0.0")

        after_pyproject = (app_dir / "pyproject.toml").read_text()
        assert after_pyproject == original_pyproject

    def test_temp_pyproject_has_rewritten_deps(self, tmp_path, target, monkeypatch):
        """The pyproject.toml in the temp dir has versioned deps, not path deps."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        import tomlkit

        temp_pyproject_content = []

        def fake_subprocess_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            if "rlsbl-build-" in cwd:
                pp = os.path.join(cwd, "pyproject.toml")
                with open(pp, "r") as f:
                    temp_pyproject_content.append(f.read())
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        target.build(str(app_dir), "1.0.0")

        assert len(temp_pyproject_content) == 1
        doc = tomlkit.parse(temp_pyproject_content[0])
        deps = list(doc["project"]["dependencies"])
        assert deps[0] == "lib>=0.5.0"
        assert deps[1] == "requests>=2.0"

    def test_excludes_git_and_pycache(self, tmp_path, target, monkeypatch):
        """The temp copy excludes .git, __pycache__, .rlsbl, dist."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        # Create directories that should be excluded
        (app_dir / ".git").mkdir()
        (app_dir / ".git" / "config").write_text("fake")
        (app_dir / "__pycache__").mkdir()
        (app_dir / "__pycache__" / "foo.pyc").write_text("fake")
        (app_dir / ".rlsbl").mkdir()
        (app_dir / ".rlsbl" / "version").write_text("0.1.0")
        (app_dir / "dist").mkdir()
        (app_dir / "dist" / "old.whl").write_text("fake")

        # Capture directory listing during build (before temp dir is cleaned up)
        captured = {}

        def fake_subprocess_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            if "rlsbl-build-" in cwd:
                captured["has_git"] = os.path.exists(os.path.join(cwd, ".git"))
                captured["has_pycache"] = os.path.exists(os.path.join(cwd, "__pycache__"))
                captured["has_rlsbl"] = os.path.exists(os.path.join(cwd, ".rlsbl"))
                captured["has_dist"] = os.path.exists(os.path.join(cwd, "dist"))
                captured["has_source"] = os.path.exists(os.path.join(cwd, "app", "__init__.py"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        target.build(str(app_dir), "1.0.0")

        assert captured, "subprocess.run was not called with a temp dir"
        assert not captured["has_git"]
        assert not captured["has_pycache"]
        assert not captured["has_rlsbl"]
        assert not captured["has_dist"]
        assert captured["has_source"]

    def test_build_failure_still_cleans_up(self, tmp_path, target, monkeypatch):
        """If uv build fails, the temp dir is still cleaned up."""
        _workspace, app_dir, _lib_dir = _make_workspace(tmp_path)

        temp_dirs_seen = []

        def fake_subprocess_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            if "rlsbl-build-" in cwd:
                temp_dirs_seen.append(cwd)
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="build failed")

        monkeypatch.setattr("subprocess.run", fake_subprocess_run)

        with pytest.raises(subprocess.CalledProcessError):
            target.build(str(app_dir), "1.0.0")

        assert len(temp_dirs_seen) == 1
        assert not os.path.exists(temp_dirs_seen[0])

    def test_rewrite_map_empty_falls_back_to_normal_build(
        self, tmp_path, target, monkeypatch
    ):
        """If path deps exist but rewrite map is empty, fall back to normal build."""
        _workspace, app_dir, lib_dir = _make_workspace(tmp_path)

        # Remove versions from ALL projects so rewrite_map is truly empty
        (app_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "app"
            dependencies = [
                "lib @ {root:uri}/../lib",
                "requests>=2.0",
            ]
        """))
        (lib_dir / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "lib"
        """))

        calls = []

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            calls.append((cmd, args, cwd))
            return ""

        monkeypatch.setattr("rlsbl.targets.pypi.run", fake_run)

        target.build(str(app_dir), "1.0.0")

        # Should fall back to normal uv build (via run(), not subprocess.run)
        assert len(calls) == 1
        assert calls[0][0] == "uv"
        assert calls[0][1][0] == "build"
        assert calls[0][2] == str(app_dir)
