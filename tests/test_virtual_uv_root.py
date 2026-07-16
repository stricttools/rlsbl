"""Tests for virtual uv workspace root handling (Phase 5.1).

A virtual root is a pyproject.toml declaring [tool.uv.workspace] with no
[project] table. It is not a package: pypi.detect() must refuse it and the
version/name/config/publish project checks must SKIP with a clear reason
instead of hard-failing on a missing version or config key.
"""

from conftest import make_ctx

from rlsbl import app
from rlsbl.utils import is_virtual_uv_root
from rlsbl.targets.pypi import PypiTarget


VIRTUAL_ROOT_PYPROJECT = """\
[tool.uv.workspace]
members = ["pkg_a", "pkg_b"]

[dependency-groups]
dev = ["pytest"]
"""

REAL_PACKAGE_PYPROJECT = """\
[project]
name = "realpkg"
version = "1.2.3"

[tool.uv.workspace]
members = ["pkg_a"]
"""

PLAIN_PACKAGE_PYPROJECT = """\
[project]
name = "plainpkg"
version = "0.1.0"
"""


class TestIsVirtualUvRoot:
    def test_virtual_root_true(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(VIRTUAL_ROOT_PYPROJECT)
        assert is_virtual_uv_root(str(tmp_path)) is True

    def test_real_package_with_workspace_false(self, tmp_path):
        """A [project] table present -> not virtual even with a workspace."""
        (tmp_path / "pyproject.toml").write_text(REAL_PACKAGE_PYPROJECT)
        assert is_virtual_uv_root(str(tmp_path)) is False

    def test_plain_package_false(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(PLAIN_PACKAGE_PYPROJECT)
        assert is_virtual_uv_root(str(tmp_path)) is False

    def test_no_pyproject_false(self, tmp_path):
        assert is_virtual_uv_root(str(tmp_path)) is False

    def test_unparseable_pyproject_false(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("this is { not valid toml")
        assert is_virtual_uv_root(str(tmp_path)) is False


class TestPypiDetectRefusesVirtualRoot:
    def test_virtual_root_not_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(VIRTUAL_ROOT_PYPROJECT)
        assert PypiTarget().detect(str(tmp_path)) is False

    def test_real_package_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(PLAIN_PACKAGE_PYPROJECT)
        assert PypiTarget().detect(str(tmp_path)) is True

    def test_real_package_with_workspace_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(REAL_PACKAGE_PYPROJECT)
        assert PypiTarget().detect(str(tmp_path)) is True


class TestVirtualRootCheckSkips:
    """The package/publish-oriented project checks must SKIP at a virtual root."""

    SKIPPING_CHECKS = [
        "target-version-readable",
        "name-consistency",
        "version-consistency",
        "config-schema",
        "publish-mode-workflow",
    ]

    def test_all_skip_at_virtual_root(self, tmp_project):
        (tmp_project / "pyproject.toml").write_text(VIRTUAL_ROOT_PYPROJECT)
        ctx = make_ctx(tmp_project)
        for name in self.SKIPPING_CHECKS:
            result = app._check_defs[name].impl(ctx)
            assert result.status == "skip", f"{name} did not skip: {result.status} {result.message}"
            assert "virtual uv workspace root" in result.message, name

    def test_real_package_does_not_skip_for_virtual_reason(self, tmp_project):
        """A genuine package must not receive the virtual-root skip."""
        import json
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test", "version": "1.0.0"})
        )
        ctx = make_ctx(tmp_project)
        result = app._check_defs["target-version-readable"].impl(ctx)
        assert result.status == "pass"
