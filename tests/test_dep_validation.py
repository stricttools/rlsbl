"""Tests for rlsbl.dep_validation -- unused, undeclared, scope, and dead module checks."""

import os
from pathlib import Path

import pytest

from rlsbl.dep_validation import (
    check_dev_in_lib,
    check_runtime_test_only,
    check_undeclared_deps,
    check_unused_deps,
    find_dead_modules,
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


class TestCheckRuntimeTestOnly:
    """check_runtime_test_only flags runtime deps used only in test code."""

    def test_runtime_dep_in_test_only_flagged(self):
        """Runtime dep imported only in tests is flagged."""
        flagged = check_runtime_test_only(
            {"auth": "runtime", "models": "runtime"},
            lib_imports={"models"},
            test_imports={"auth", "models"},
        )
        assert flagged == ["auth"]

    def test_runtime_dep_in_lib_not_flagged(self):
        """Runtime dep imported in lib is not flagged."""
        flagged = check_runtime_test_only(
            {"auth": "runtime"},
            lib_imports={"auth"},
            test_imports={"auth"},
        )
        assert flagged == []

    def test_runtime_dep_in_both_not_flagged(self):
        """Runtime dep imported in both lib and test is not flagged."""
        flagged = check_runtime_test_only(
            {"auth": "runtime"},
            lib_imports={"auth"},
            test_imports={"auth"},
        )
        assert flagged == []

    def test_dev_dep_not_checked(self):
        """Dev deps are ignored by this check."""
        flagged = check_runtime_test_only(
            {"testutils": "dev"},
            lib_imports=set(),
            test_imports={"testutils"},
        )
        assert flagged == []

    def test_runtime_dep_not_imported_anywhere(self):
        """Runtime dep not imported at all is not flagged (unused check handles it)."""
        flagged = check_runtime_test_only(
            {"auth": "runtime"},
            lib_imports=set(),
            test_imports=set(),
        )
        assert flagged == []

    def test_empty_deps(self):
        """Empty deps produce no flags."""
        flagged = check_runtime_test_only({}, set(), set())
        assert flagged == []

    def test_multiple_flagged_sorted(self):
        """Multiple flagged deps are returned in sorted order."""
        flagged = check_runtime_test_only(
            {"zebra": "runtime", "alpha": "runtime"},
            lib_imports=set(),
            test_imports={"zebra", "alpha"},
        )
        assert flagged == ["alpha", "zebra"]


class TestCheckDevInLib:
    """check_dev_in_lib flags dev deps imported in production code."""

    def test_dev_dep_in_lib_flagged(self):
        """Dev dep imported in lib is flagged."""
        flagged = check_dev_in_lib(
            {"testutils": "dev"},
            lib_imports={"testutils"},
        )
        assert flagged == ["testutils"]

    def test_dev_dep_not_in_lib_not_flagged(self):
        """Dev dep not imported in lib is not flagged."""
        flagged = check_dev_in_lib(
            {"testutils": "dev"},
            lib_imports=set(),
        )
        assert flagged == []

    def test_runtime_dep_ignored(self):
        """Runtime deps are ignored by this check."""
        flagged = check_dev_in_lib(
            {"auth": "runtime"},
            lib_imports={"auth"},
        )
        assert flagged == []

    def test_empty_deps(self):
        """Empty deps produce no flags."""
        flagged = check_dev_in_lib({}, set())
        assert flagged == []

    def test_multiple_flagged_sorted(self):
        """Multiple flagged deps are returned in sorted order."""
        flagged = check_dev_in_lib(
            {"zebra": "dev", "alpha": "dev"},
            lib_imports={"zebra", "alpha"},
        )
        assert flagged == ["alpha", "zebra"]


class TestFindDeadModules:
    """find_dead_modules detects unreferenced Python modules."""

    def test_unreferenced_module_flagged(self, tmp_path):
        """A module not imported by anything is flagged as dead."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (pkg / "unused.py").write_text("y = 2\n")
        # core.py is imported by __init__.py
        (pkg / "__init__.py").write_text("from .core import x\n")

        dead = find_dead_modules(str(tmp_path))
        assert "mylib/unused.py" in dead
        assert "mylib/core.py" not in dead

    def test_referenced_module_not_flagged(self, tmp_path):
        """A module imported by another is not flagged."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("from .utils import helper\n")
        (pkg / "utils.py").write_text("def helper(): pass\n")

        dead = find_dead_modules(str(tmp_path))
        # Both are referenced (core imports utils, utils is imported)
        assert "mylib/utils.py" not in dead

    def test_init_all_exports_not_flagged(self, tmp_path):
        """Modules listed in __init__.py __all__ are not flagged."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("__all__ = ['api']\n")
        (pkg / "api.py").write_text("def run(): pass\n")

        dead = find_dead_modules(str(tmp_path))
        assert "mylib/api.py" not in dead

    def test_test_files_excluded(self, tmp_path):
        """Test files are excluded from the dead module scan."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import x\n")
        (pkg / "core.py").write_text("x = 1\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_core.py").write_text("import mylib.core\n")

        dead = find_dead_modules(str(tmp_path))
        # test_core.py should not appear as a dead module
        rel_paths = [os.path.basename(d) for d in dead]
        assert "test_core.py" not in rel_paths

    def test_non_python_project_returns_empty(self, tmp_path):
        """Non-Python project (no pyproject.toml) returns empty list."""
        (tmp_path / "main.go").write_text("package main\n")
        dead = find_dead_modules(str(tmp_path))
        assert dead == []

    def test_no_production_files_returns_empty(self, tmp_path):
        """Project with only test files returns empty list."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_something.py").write_text("assert True\n")

        dead = find_dead_modules(str(tmp_path))
        assert dead == []

    def test_module_imported_by_parent_prefix(self, tmp_path):
        """A module whose parent package is imported is considered referenced."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        sub = pkg / "sub"
        sub.mkdir()
        (pkg / "__init__.py").write_text("")
        (sub / "__init__.py").write_text("")
        (sub / "detail.py").write_text("z = 3\n")
        (pkg / "main.py").write_text("import mylib.sub.detail\n")

        dead = find_dead_modules(str(tmp_path))
        assert "mylib/sub/detail.py" not in dead


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

    def test_deps_runtime_test_only_registered(self):
        """deps-runtime-test-only check is registered."""
        captured = self._capture_checks()
        assert "deps-runtime-test-only" in captured

    def test_deps_dev_in_lib_registered(self):
        """deps-dev-in-lib check is registered."""
        captured = self._capture_checks()
        assert "deps-dev-in-lib" in captured

    def test_deps_runtime_test_only_skip_not_workspace(self):
        """deps-runtime-test-only skips when context is not a workspace."""
        from rlsbl.context import ProjectContext

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = captured["deps-runtime-test-only"](ctx)
        assert result.status == "skip"

    def test_deps_dev_in_lib_skip_not_workspace(self):
        """deps-dev-in-lib skips when context is not a workspace."""
        from rlsbl.context import ProjectContext

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = captured["deps-dev-in-lib"](ctx)
        assert result.status == "skip"

    def test_deps_runtime_test_only_warns(self, tmp_path):
        """deps-runtime-test-only warns when a runtime dep is test-only."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "auth"\nname = "auth"\n'
        )

        # App declares auth as runtime dep but only imports it in tests
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["auth"]\n'
        )
        (app_dir / "main.py").write_text("import os\n")
        tests_dir = app_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_it.py").write_text("import auth\n")

        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "pyproject.toml").write_text('[project]\nname = "auth"\n')

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
        result = captured["deps-runtime-test-only"](ctx)
        assert result.status == "warn"
        assert "1 runtime dep" in result.message

    def test_deps_dev_in_lib_fails(self, tmp_path):
        """deps-dev-in-lib fails when a dev dep is imported in production."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "testutils"\nname = "testutils"\n'
        )

        # App declares testutils as dev dep but imports it in production code
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\n\n'
            '[project.optional-dependencies]\ndev = ["testutils"]\n'
        )
        (app_dir / "main.py").write_text("import testutils\n")

        tu_dir = tmp_path / "testutils"
        tu_dir.mkdir()
        (tu_dir / "pyproject.toml").write_text('[project]\nname = "testutils"\n')

        projects = [
            {"name": "app", "path": "app"},
            {"name": "testutils", "path": "testutils"},
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
        result = captured["deps-dev-in-lib"](ctx)
        assert result.status == "fail"
        assert "1 dev dep" in result.message

    def test_dead_modules_registered(self):
        """dead-modules check is registered."""
        captured = self._capture_checks()
        assert "dead-modules" in captured

    def test_dead_modules_skip_non_python(self, tmp_path):
        """dead-modules skips for non-Python projects."""
        from rlsbl.context import ProjectContext

        # Create a non-Python project (npm only)
        (tmp_path / "package.json").write_text('{"name":"test","version":"1.0.0"}')

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "skip"

    def test_dead_modules_pass_clean(self, tmp_path):
        """dead-modules passes when all modules are referenced."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import run\n")
        (pkg / "core.py").write_text("def run(): pass\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "pass"

    def test_dead_modules_warn_unreferenced(self, tmp_path):
        """dead-modules warns when a module is unreferenced."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import run\n")
        (pkg / "core.py").write_text("def run(): pass\n")
        (pkg / "orphan.py").write_text("def unused(): pass\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "warn"
        assert "1 dead module" in result.message
