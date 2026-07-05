"""Tests for per-target CI workflow generation in run_cmd_multi."""

import json
import os
from io import StringIO
from unittest.mock import patch

import pytest

from rlsbl.commands.init_cmd import run_cmd_multi, _is_npm_wrapper
from rlsbl.context import ProjectContext


def _ctx(root="."):
    """Create a minimal ProjectContext for scaffold tests."""
    from pathlib import Path
    return ProjectContext(project_root=Path(root), workspace_root=None, config={})


class TestPerTargetCI:
    """Integration tests for per-target CI workflow generation."""

    def test_multi_target_generates_per_target_ci(self, mock_git_repo):
        """pypi+go scaffold produces ci-pypi.yml and ci-go.yml, not ci.yml."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "multi-ci-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "go.mod").write_text(
            "module github.com/test/multi-ci-test\n\ngo 1.23\n"
        )

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "go"], [], {}, ctx=_ctx())

        ci_pypi = os.path.join(".github", "workflows", "ci-pypi.yml")
        ci_go = os.path.join(".github", "workflows", "ci-go.yml")
        ci_generic = os.path.join(".github", "workflows", "ci.yml")

        assert os.path.exists(ci_pypi)
        assert os.path.exists(ci_go)
        assert not os.path.exists(ci_generic)

        with open(ci_pypi) as f:
            pypi_content = f.read()
        assert "pytest" in pypi_content or "uv" in pypi_content

        with open(ci_go) as f:
            go_content = f.read()
        assert "go test" in go_content

    def test_npm_wrapper_skips_ci(self, mock_git_repo):
        """npm wrapper (no test script) does not get a CI workflow."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "wrapper-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        pkg = {"name": "wrapper-test", "version": "0.1.0"}
        (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd_multi(["pypi", "npm"], [], {}, ctx=_ctx())

        ci_pypi = os.path.join(".github", "workflows", "ci-pypi.yml")
        ci_npm = os.path.join(".github", "workflows", "ci-npm.yml")

        assert os.path.exists(ci_pypi)
        assert not os.path.exists(ci_npm)
        assert "Skipping npm CI (no test script in package.json)" in mock_stdout.getvalue()

    def test_npm_with_test_script_gets_ci(self, mock_git_repo):
        """npm package with a test script gets its own CI workflow."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "testable-pkg"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        pkg = {
            "name": "testable-pkg",
            "version": "0.1.0",
            "scripts": {"test": "jest"},
            "devDependencies": {"jest": "^29"},
        }
        (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        (root / "package-lock.json").write_text("{}\n")

        with patch("sys.stdout", new_callable=StringIO):
            run_cmd_multi(["pypi", "npm"], [], {}, ctx=_ctx())

        ci_pypi = os.path.join(".github", "workflows", "ci-pypi.yml")
        ci_npm = os.path.join(".github", "workflows", "ci-npm.yml")

        assert os.path.exists(ci_pypi)
        assert os.path.exists(ci_npm)

    def test_old_ci_yml_cleaned_up(self, mock_git_repo):
        """Pre-existing ci.yml and its base are removed when per-target CI is generated."""
        root = mock_git_repo

        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "multi-ci-test"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (root / "go.mod").write_text(
            "module github.com/test/multi-ci-test\n\ngo 1.23\n"
        )

        # Pre-create old ci.yml and its base
        os.makedirs(os.path.join(".github", "workflows"), exist_ok=True)
        with open(os.path.join(".github", "workflows", "ci.yml"), "w") as f:
            f.write("old ci content\n")

        os.makedirs(os.path.join(".rlsbl", "bases", ".github", "workflows"), exist_ok=True)
        with open(os.path.join(".rlsbl", "bases", ".github", "workflows", "ci.yml"), "w") as f:
            f.write("old base\n")

        # Register ci.yml in both registries (simulates prior single-target scaffold)
        from rlsbl.commands.init_cmd import file_hash, save_managed_files
        ci_entry = {os.path.join(".github", "workflows", "ci.yml"): file_hash(os.path.join(".github", "workflows", "ci.yml"))}
        save_managed_files(ci_entry)

        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            run_cmd_multi(["pypi", "go"], [], {}, ctx=_ctx())

        assert not os.path.exists(os.path.join(".github", "workflows", "ci.yml"))
        assert not os.path.exists(
            os.path.join(".rlsbl", "bases", ".github", "workflows", "ci.yml")
        )
        assert os.path.exists(os.path.join(".github", "workflows", "ci-pypi.yml"))
        assert os.path.exists(os.path.join(".github", "workflows", "ci-go.yml"))
        assert "removed (orphan)" in mock_stdout.getvalue()


class TestNpmCILockfileConditional:
    """Tests that CI templates render with lockfile-conditional install steps."""

    def test_npm_ci_template_has_lockfile_conditional(self):
        """npm ci.yml.tpl renders with conditional lockfile check."""
        from rlsbl.commands.init_cmd import process_template
        from rlsbl.targets.npm import NpmTarget

        target = NpmTarget()
        tpl_dir = target.template_dir()
        tpl_path = os.path.join(tpl_dir, "ci.yml.tpl")

        with open(tpl_path) as f:
            raw = f.read()

        content, _ = process_template(raw, {"npm.minRequiredNode": "20"})
        assert "if [ -f package-lock.json ]" in content
        assert "npm ci" in content
        assert "npm install" in content

    def test_pnpm_ci_template_has_lockfile_conditional(self):
        """pnpm ci template renders with conditional lockfile check."""
        from rlsbl.commands.init_cmd import process_template
        from rlsbl.targets.npm import NpmTarget

        target = NpmTarget()
        tpl_dir = target.template_dir()
        tpl_path = os.path.join(tpl_dir, "ci-pnpm.yml.tpl")

        with open(tpl_path) as f:
            raw = f.read()

        content, _ = process_template(raw, {"npm.minRequiredNode": "20", "npm.name": "my-pkg"})
        assert "if [ -f pnpm-lock.yaml ]" in content
        assert "pnpm install --frozen-lockfile" in content
        # Workspace root fallback: --filter for workspace lockfile
        assert 'GITHUB_WORKSPACE/pnpm-lock.yaml' in content
        assert "--frozen-lockfile --filter" in content
        # Hard error when no lockfile found
        assert "::error::" in content

    def test_pnpm_ci_template_workspace_filter_uses_package_name(self):
        """pnpm workspace fallback uses the correct package name in --filter."""
        from rlsbl.commands.init_cmd import process_template
        from rlsbl.targets.npm import NpmTarget

        target = NpmTarget()
        tpl_dir = target.template_dir()
        tpl_path = os.path.join(tpl_dir, "ci-pnpm.yml.tpl")

        with open(tpl_path) as f:
            raw = f.read()

        content, _ = process_template(raw, {"npm.name": "@scope/my-package"})
        assert "--filter @scope/my-package" in content

    def test_yarn_ci_template_has_lockfile_conditional(self):
        """yarn ci template renders with conditional lockfile check."""
        from rlsbl.commands.init_cmd import process_template
        from rlsbl.targets.npm import NpmTarget

        target = NpmTarget()
        tpl_dir = target.template_dir()
        tpl_path = os.path.join(tpl_dir, "ci-yarn.yml.tpl")

        with open(tpl_path) as f:
            raw = f.read()

        content, _ = process_template(raw, {"npm.minRequiredNode": "20"})
        assert "if [ -f yarn.lock ]" in content
        assert "yarn install --frozen-lockfile" in content
        assert "yarn install" in content


class TestIsNpmWrapper:
    """Unit tests for _is_npm_wrapper."""

    def test_is_npm_wrapper_no_scripts(self, tmp_project):
        """package.json with no scripts field is a wrapper."""
        pkg = {"name": "test"}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is True

    def test_is_npm_wrapper_empty_test(self, tmp_project):
        """package.json with empty test script is a wrapper."""
        pkg = {"name": "test", "scripts": {"test": ""}}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is True

    def test_is_npm_wrapper_has_test(self, tmp_project):
        """package.json with a real test script and deps is NOT a wrapper."""
        pkg = {"name": "test", "scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is False

    def test_is_npm_wrapper_zero_deps(self, tmp_project):
        """package.json with a test script but zero dependencies is a wrapper."""
        pkg = {"name": "test", "scripts": {"test": "python3 -m pytest tests/"}}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is True

    def test_is_npm_wrapper_empty_deps(self, tmp_project):
        """package.json with empty dependencies objects is a wrapper."""
        pkg = {
            "name": "test",
            "scripts": {"test": "python3 -m pytest"},
            "dependencies": {},
            "devDependencies": {},
        }
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is True

    def test_is_npm_wrapper_has_deps_only(self, tmp_project):
        """package.json with test script and runtime deps is NOT a wrapper."""
        pkg = {"name": "test", "scripts": {"test": "jest"}, "dependencies": {"lodash": "^4"}}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is False

    def test_is_npm_wrapper_has_dev_deps_only(self, tmp_project):
        """package.json with test script and devDependencies is NOT a wrapper."""
        pkg = {"name": "test", "scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}
        (tmp_project / "package.json").write_text(json.dumps(pkg) + "\n")
        assert _is_npm_wrapper(".") is False
