"""Tests for import_name auto-population during monorepo sync."""

import os
import subprocess

import tomlkit
import pytest

from rlsbl.commands.monorepo import _cmd_init, _sync_import_names
from rlsbl.workspace import load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE, WorkspaceProject


def _make_python_project_with_hatch(root, subdir, project_name, pkg_dir_name):
    """Create a Python project whose hatch config declares a mismatched package path.

    This simulates projects like cloudflare (imports as cf) where the
    build tool config explicitly declares the package location.
    """
    proj_dir = os.path.join(str(root), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg_path = os.path.join(proj_dir, pkg_dir_name)
    os.makedirs(pkg_path, exist_ok=True)
    with open(os.path.join(pkg_path, "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(
            f'[project]\nname = "{project_name}"\nversion = "0.1.0"\n\n'
            f'[tool.hatch.build.targets.wheel]\npackages = ["{pkg_dir_name}"]\n'
        )


def _make_python_project_flat(root, subdir, project_name, pkg_dir_name=None):
    """Create a Python project with a flat-layout package directory.

    If pkg_dir_name is not provided, uses the underscored project name
    (standard convention: matches, so no import_name needed).
    """
    if pkg_dir_name is None:
        pkg_dir_name = project_name.replace("-", "_")
    proj_dir = os.path.join(str(root), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
        f.write(f'[project]\nname = "{project_name}"\nversion = "0.1.0"\n')
    pkg_path = os.path.join(proj_dir, pkg_dir_name)
    os.makedirs(pkg_path, exist_ok=True)
    with open(os.path.join(pkg_path, "__init__.py"), "w") as f:
        f.write("")


def _init_workspace_manual(root, project_specs):
    """Initialize workspace and add projects by writing workspace.toml directly.

    Avoids _cmd_add which triggers scaffold+sync (which would
    auto-populate import_name before the test can exercise it).

    project_specs: list of dicts with at least 'path' and 'name'.
    """
    _cmd_init({}, project_root=".")
    projects = [WorkspaceProject(spec) for spec in project_specs]
    save_workspace(str(root), projects)
    subprocess.run(["git", "add", "."], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=str(root), check=True)


class TestSyncImportNames:
    """Tests for _sync_import_names auto-detection."""

    def test_mismatched_import_name_detected(self, mock_git_repo):
        """When hatch config declares a package dir that differs from the project name, import_name is written."""
        _make_python_project_with_hatch(mock_git_repo, "cloudflare", "cloudflare", "cf")
        _init_workspace_manual(mock_git_repo, [
            {"path": "cloudflare", "name": "cloudflare"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is not None
        projects_after = load_workspace(str(mock_git_repo))
        cf_proj = [p for p in projects_after if p.name == "cloudflare"][0]
        assert cf_proj.import_name == "cf"

    def test_matching_name_skipped(self, mock_git_repo):
        """When import name matches the underscored project name, no import_name is written."""
        _make_python_project_flat(mock_git_repo, "my-package", "my-package")
        _init_workspace_manual(mock_git_repo, [
            {"path": "my-package", "name": "my-package"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is None
        projects_after = load_workspace(str(mock_git_repo))
        pkg_proj = [p for p in projects_after if p.name == "my-package"][0]
        assert pkg_proj.import_name == ""

    def test_existing_import_name_preserved(self, mock_git_repo):
        """When import_name is already set, it is not overwritten even if detection would differ."""
        _make_python_project_with_hatch(mock_git_repo, "mypkg", "mypkg", "mp")
        _init_workspace_manual(mock_git_repo, [
            {"path": "mypkg", "name": "mypkg", "import_name": "custom_override"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        # No changes: existing import_name is respected
        assert result is None
        projects_after = load_workspace(str(mock_git_repo))
        pkg_proj = [p for p in projects_after if p.name == "mypkg"][0]
        assert pkg_proj.import_name == "custom_override"

    def test_non_python_project_skipped(self, mock_git_repo):
        """Non-Python projects (no pyproject.toml) are skipped."""
        proj_dir = os.path.join(str(mock_git_repo), "gomod")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, "go.mod"), "w") as f:
            f.write("module example.com/gomod\n\ngo 1.21\n")
        _init_workspace_manual(mock_git_repo, [
            {"path": "gomod", "name": "gomod"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is None

    def test_no_package_root_detected_skipped(self, mock_git_repo):
        """When detect_python_package_root returns None, the project is skipped."""
        proj_dir = os.path.join(str(mock_git_repo), "emptypkg")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "emptypkg"\nversion = "0.1.0"\n')
        # No package directory created
        _init_workspace_manual(mock_git_repo, [
            {"path": "emptypkg", "name": "emptypkg"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is None

    def test_multiple_projects_selective(self, mock_git_repo):
        """Only projects with mismatched names get import_name; matching ones are skipped."""
        _make_python_project_with_hatch(mock_git_repo, "cloudflare", "cloudflare", "cf")
        _make_python_project_flat(mock_git_repo, "normal-pkg", "normal-pkg")
        _init_workspace_manual(mock_git_repo, [
            {"path": "cloudflare", "name": "cloudflare"},
            {"path": "normal-pkg", "name": "normal-pkg"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is not None
        projects_after = load_workspace(str(mock_git_repo))
        cf_proj = [p for p in projects_after if p.name == "cloudflare"][0]
        normal_proj = [p for p in projects_after if p.name == "normal-pkg"][0]
        assert cf_proj.import_name == "cf"
        assert normal_proj.import_name == ""

    def test_src_layout_import_name(self, mock_git_repo):
        """src layout via hatch: import_name is derived from the basename of the package root."""
        proj_dir = os.path.join(str(mock_git_repo), "mypkg")
        os.makedirs(proj_dir, exist_ok=True)
        src_pkg = os.path.join(proj_dir, "src", "mp")
        os.makedirs(src_pkg, exist_ok=True)
        with open(os.path.join(src_pkg, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write(
                '[project]\nname = "mypkg"\nversion = "0.1.0"\n\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["src/mp"]\n'
            )
        _init_workspace_manual(mock_git_repo, [
            {"path": "mypkg", "name": "mypkg"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        assert result is not None
        projects_after = load_workspace(str(mock_git_repo))
        pkg_proj = [p for p in projects_after if p.name == "mypkg"][0]
        assert pkg_proj.import_name == "mp"

    def test_uv_build_backend_module_root(self, mock_git_repo):
        """uv build-backend with module-root: import_name detected from src layout."""
        proj_dir = os.path.join(str(mock_git_repo), "mylib")
        os.makedirs(proj_dir, exist_ok=True)
        src_pkg = os.path.join(proj_dir, "lib", "mylib")
        os.makedirs(src_pkg, exist_ok=True)
        with open(os.path.join(src_pkg, "__init__.py"), "w") as f:
            f.write("")
        # Project name matches the underscored name but via non-standard module-root
        # detect_python_package_root returns "lib/mylib", basename is "mylib" which
        # matches the underscored name -> no import_name needed
        with open(os.path.join(proj_dir, "pyproject.toml"), "w") as f:
            f.write(
                '[project]\nname = "mylib"\nversion = "0.1.0"\n\n'
                '[tool.uv.build-backend]\nmodule-root = "lib"\n'
            )
        _init_workspace_manual(mock_git_repo, [
            {"path": "mylib", "name": "mylib"},
        ])

        projects = load_workspace(str(mock_git_repo))
        result = _sync_import_names(str(mock_git_repo), projects)

        # mylib matches, no import_name needed
        assert result is None
