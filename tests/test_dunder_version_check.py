"""Tests for the dunder-version-missing project check."""

import json
import os

from conftest import make_ctx

from rlsbl import app


def _setup_pypi_project(tmp_project, init_content="", *, pkg_name="mypkg"):
    """Create a minimal pypi project with .rlsbl config and package dir.

    Returns the path to the package's __init__.py.
    """
    # .rlsbl/config.json with pypi target
    rlsbl_dir = tmp_project / ".rlsbl"
    rlsbl_dir.mkdir(parents=True, exist_ok=True)
    (rlsbl_dir / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
    )

    # pyproject.toml
    (tmp_project / "pyproject.toml").write_text(
        f'[project]\nname = "{pkg_name}"\nversion = "1.0.0"\n'
    )

    # Package directory with __init__.py
    pkg_dir = tmp_project / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    init_path = pkg_dir / "__init__.py"
    init_path.write_text(init_content)

    return init_path


class TestDunderVersionMissing:
    """Tests for the dunder-version-missing check."""

    def test_static_dunder_version_passes(self, tmp_project):
        """pypi target with static __version__ = '1.0.0' returns pass."""
        _setup_pypi_project(tmp_project, '__version__ = "1.0.0"\n')
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "__version__" in result.message

    def test_dynamic_dunder_version_passes(self, tmp_project):
        """pypi target with dynamic __version__ = get_version() returns pass."""
        _setup_pypi_project(tmp_project, "__version__ = get_version()\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "__version__" in result.message

    def test_imported_dunder_version_passes(self, tmp_project):
        """pypi target with 'from ._version import __version__' returns pass."""
        _setup_pypi_project(
            tmp_project, "from ._version import __version__\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "__version__" in result.message

    def test_version_constant_without_dunder_fails(self, tmp_project):
        """pypi target with VERSION = '1.0.0' but no __version__ returns fail."""
        _setup_pypi_project(tmp_project, 'VERSION = "1.0.0"\n')
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "fail"
        assert "VERSION" in result.message
        assert "__version__" in result.message
        assert "rename" in result.message

    def test_no_version_constant_passes(self, tmp_project):
        """pypi target with no version constant at all returns pass."""
        _setup_pypi_project(
            tmp_project, "from .core import main\n\n__all__ = ['main']\n"
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "no version constant" in result.message

    def test_non_pypi_target_skips(self, tmp_project):
        """Non-pypi target (go target only) returns skip."""
        # Set up a go-only project
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir(parents=True, exist_ok=True)
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["go"]}) + "\n"
        )
        (tmp_project / "go.mod").write_text(
            "module example.com/mymod\n\ngo 1.21\n"
        )
        (tmp_project / "VERSION").write_text("1.0.0\n")
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "skip"
        assert "no pypi target" in result.message

    def test_no_init_py_passes(self, tmp_project):
        """pypi target with no __init__.py returns pass (namespace package)."""
        # Set up pypi project without creating __init__.py
        rlsbl_dir = tmp_project / ".rlsbl"
        rlsbl_dir.mkdir(parents=True, exist_ok=True)
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
        )
        (tmp_project / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "1.0.0"\n'
        )
        # Create package dir but no __init__.py
        pkg_dir = tmp_project / "mypkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "namespace" in result.message or "__init__" in result.message

    def test_schema_version_not_false_positive(self, tmp_project):
        """SCHEMA_VERSION = '3' (not semver-like) should not false-positive."""
        _setup_pypi_project(tmp_project, 'SCHEMA_VERSION = "3"\n')
        ctx = make_ctx(tmp_project)
        result = app._check_defs["dunder-version-missing"].impl(ctx)
        assert result.status == "pass"
        assert "no version constant" in result.message
