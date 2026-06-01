"""Tests for rlsbl.dep_validation -- unused and undeclared dependency checks."""

import os
from pathlib import Path

import pytest

from rlsbl.dep_validation import (
    check_undeclared_deps,
    check_unused_deps,
    load_dep_overrides,
)
from rlsbl.workspace import WORKSPACE_DIR


# Minimal pyproject.toml so the Python import scanner finds .py files
_PYPROJECT = '[project]\nname = "example"\n'


class TestCheckUnusedDeps:
    """check_unused_deps detects declared-but-unimported workspace deps."""

    def test_declared_dep_not_imported_is_error(self, tmp_path):
        """A declared dependency with no matching import produces an error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import os\n")

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]
        assert "no source file imports it" in errors[0]

    def test_declared_dep_imported_no_error(self, tmp_path):
        """A declared dependency that is imported produces no error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import auth\n")

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_whitelisted_unused_dep_no_error(self, tmp_path):
        """A whitelisted unused dependency produces no error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import os\n")

        whitelist = {("app", "auth"): "Wired via DI at runtime"}
        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist=whitelist,
        )
        assert errors == []

    def test_empty_manifest_deps_no_errors(self, tmp_path):
        """No declared deps means no possible unused deps."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_test_only_import_counts(self, tmp_path):
        """A dep imported only in test context still counts as used."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        tests_dir = project_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_it.py").write_text("import auth\n")

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_multiple_unused_deps(self, tmp_path):
        """Multiple unused deps produce multiple errors."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import os\n")

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth", "models"},
            workspace_names={"app", "auth", "models"},
            whitelist={},
        )
        assert len(errors) == 2


class TestCheckUndeclaredDeps:
    """check_undeclared_deps detects imported-but-undeclared workspace deps."""

    def test_undeclared_import_is_error(self, tmp_path):
        """Importing a workspace package not declared as dep is an error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import auth\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "auth"},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]
        assert "does not declare it as a dependency" in errors[0]

    def test_declared_import_no_error(self, tmp_path):
        """Importing a declared dependency produces no error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import auth\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
        )
        assert errors == []

    def test_test_imports_are_skipped(self, tmp_path):
        """Imports in test context do not produce undeclared errors."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        tests_dir = project_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_it.py").write_text("import auth\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "auth"},
        )
        assert errors == []

    def test_self_import_not_error(self, tmp_path):
        """Importing the project's own package is not an error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import app\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app"},
        )
        assert errors == []

    def test_non_workspace_import_ignored(self, tmp_path):
        """Imports of packages not in the workspace are ignored."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import requests\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app"},
        )
        assert errors == []


class TestLoadDepOverrides:
    """load_dep_overrides reads the whitelist config file."""

    def test_valid_file(self, tmp_path):
        """A well-formed overrides file loads correctly."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '[[unused_allowed]]\n'
            'package = "app"\n'
            'dep = "auth"\n'
            'reason = "Wired via DI at runtime"\n'
        )
        result = load_dep_overrides(str(tmp_path))
        assert result == {("app", "auth"): "Wired via DI at runtime"}

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing overrides file returns an empty dict."""
        result = load_dep_overrides(str(tmp_path))
        assert result == {}

    def test_missing_reason_raises(self, tmp_path):
        """An entry without 'reason' raises ValueError."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '[[unused_allowed]]\n'
            'package = "app"\n'
            'dep = "auth"\n'
        )
        with pytest.raises(ValueError, match="missing required key 'reason'"):
            load_dep_overrides(str(tmp_path))

    def test_empty_reason_raises(self, tmp_path):
        """An entry with empty 'reason' raises ValueError."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '[[unused_allowed]]\n'
            'package = "app"\n'
            'dep = "auth"\n'
            'reason = "  "\n'
        )
        with pytest.raises(ValueError, match="must not be empty"):
            load_dep_overrides(str(tmp_path))

    def test_missing_package_raises(self, tmp_path):
        """An entry without 'package' raises ValueError."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '[[unused_allowed]]\n'
            'dep = "auth"\n'
            'reason = "test"\n'
        )
        with pytest.raises(ValueError, match="missing required key 'package'"):
            load_dep_overrides(str(tmp_path))

    def test_multiple_entries(self, tmp_path):
        """Multiple entries are all loaded."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '[[unused_allowed]]\n'
            'package = "app"\n'
            'dep = "auth"\n'
            'reason = "DI"\n'
            '\n'
            '[[unused_allowed]]\n'
            'package = "app"\n'
            'dep = "models"\n'
            'reason = "Lazy loaded"\n'
        )
        result = load_dep_overrides(str(tmp_path))
        assert len(result) == 2
        assert ("app", "auth") in result
        assert ("app", "models") in result

    def test_no_unused_allowed_section(self, tmp_path):
        """File exists but has no unused_allowed key returns empty."""
        ws_dir = tmp_path / WORKSPACE_DIR
        ws_dir.mkdir()
        (ws_dir / "dep-overrides.toml").write_text(
            '# empty overrides file\n'
        )
        result = load_dep_overrides(str(tmp_path))
        assert result == {}


class TestNpmWorkspaceDep:
    """npm-specific dependency validation tests."""

    def test_npm_workspace_dep_not_false_positive(self, tmp_path):
        """npm project with workspace:* dep that IS imported is not flagged as unused."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "index.js").write_text(
            "const auth = require('auth');\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_npm_workspace_dep_unused_is_flagged(self, tmp_path):
        """npm project with workspace:* dep that is NOT imported is flagged."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "index.js").write_text(
            "const express = require('express');\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]

    def test_npm_scoped_workspace_dep_imported(self, tmp_path):
        """Scoped npm workspace dep that is imported is not flagged."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "index.ts").write_text(
            "import { helper } from '@myorg/utils';\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"@myorg/utils"},
            workspace_names={"app", "@myorg/utils"},
            whitelist={},
        )
        assert errors == []


class TestDepsChecksIntegration:
    """Integration tests: checks registered on the strictcli check system."""

    def _capture_checks(self):
        """Register all checks on a mock app and return the captured dict."""
        captured = {}

        class MockApp:
            _checks_enabled = True

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())
        return captured

    def test_deps_unused_registered(self):
        """deps-unused check is registered."""
        captured = self._capture_checks()
        assert "deps-unused" in captured

    def test_deps_undeclared_registered(self):
        """deps-undeclared check is registered."""
        captured = self._capture_checks()
        assert "deps-undeclared" in captured

    def test_deps_unused_skip_not_workspace(self):
        """deps-unused skips when context is not a workspace."""
        from strictcli import CheckResult

        from rlsbl.context import ProjectContext

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = captured["deps-unused"](ctx)
        assert result.status == "skip"

    def test_deps_undeclared_skip_not_workspace(self):
        """deps-undeclared skips when context is not a workspace."""
        from strictcli import CheckResult

        from rlsbl.context import ProjectContext

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = captured["deps-undeclared"](ctx)
        assert result.status == "skip"

    def test_deps_unused_pass_clean_workspace(self, tmp_path):
        """deps-unused passes when all declared deps are imported."""
        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        # Set up workspace
        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "auth"\nname = "auth"\n'
        )

        # App depends on auth and imports it
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["auth"]\n'
        )
        (app_dir / "main.py").write_text("import auth\n")

        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "pyproject.toml").write_text(
            '[project]\nname = "auth"\n'
        )

        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth"},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = self._capture_checks()
        result = captured["deps-unused"](ctx)
        assert result.status == "pass"

    def test_deps_unused_fail_unused_dep(self, tmp_path):
        """deps-unused fails when a declared dep is not imported."""
        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "auth"\nname = "auth"\n'
        )

        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["auth"]\n'
        )
        (app_dir / "main.py").write_text("import os\n")

        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "pyproject.toml").write_text(
            '[project]\nname = "auth"\n'
        )

        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth"},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = self._capture_checks()
        result = captured["deps-unused"](ctx)
        assert result.status == "fail"
        assert "1 unused dependency" in result.message

    def test_deps_undeclared_fail_missing_dep(self, tmp_path):
        """deps-undeclared fails when a workspace import has no declared dep."""
        from strictcli import CheckResult

        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "auth"\nname = "auth"\n'
        )

        # App imports auth but does not declare it as dependency
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\n'
        )
        (app_dir / "main.py").write_text("import auth\n")

        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "pyproject.toml").write_text(
            '[project]\nname = "auth"\n'
        )

        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth"},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = self._capture_checks()
        result = captured["deps-undeclared"](ctx)
        assert result.status == "fail"
        assert "1 undeclared dependency" in result.message
