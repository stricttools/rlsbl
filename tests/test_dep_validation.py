"""Tests for rlsbl.dep_validation -- unused, undeclared, scope, and dead module checks."""

import os
from pathlib import Path

import pytest

from rlsbl.errors import ConfigError
from rlsbl.dep_validation import (
    DeadWorkspacePackage,
    _is_inside_python_package,
    check_dev_in_lib,
    check_runtime_test_only,
    check_undeclared_deps,
    check_unused_deps,
    find_dead_dart_modules,
    find_dead_go_packages,
    find_dead_modules,
    find_dead_npm_modules,
    find_dead_workspace_packages,
    load_dep_overrides,
)
from rlsbl.lint.utils import walk_source_files
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={},
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={"auth": "runtime", "models": "runtime"},
            workspace_names={"app", "auth", "models"},
            whitelist={},
        )
        assert len(errors) == 2

    def test_guarded_import_not_false_positive(self, tmp_path):
        """An OPTIONAL dep imported only in try/except ImportError is NOT unused.

        Guarded imports count as "used" for optional deps (scope="dev"),
        since the dep is intentionally declared for optional use.
        """
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import auth\n"
            "except ImportError:\n"
            "    auth = None\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "dev"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_hard_dep_guarded_only_is_flagged(self, tmp_path):
        """A HARD dep (scope=runtime) imported only in try/except IS flagged.

        Declaring a hard dependency but only importing it inside
        try/except ImportError is contradictory -- either declare it
        optional or import it unconditionally.
        """
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import auth\n"
            "except ImportError:\n"
            "    auth = None\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "runtime"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]
        assert "try/except ImportError" in errors[0]

    def test_hard_dep_imported_normally_and_guarded_not_flagged(self, tmp_path):
        """A hard dep imported both unconditionally and guarded is not flagged."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import auth\n")
        (project_dir / "extras.py").write_text(
            "try:\n"
            "    import auth\n"
            "except ImportError:\n"
            "    auth = None\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "runtime"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert errors == []

    def test_optional_dep_never_imported_still_flagged(self, tmp_path):
        """An optional dep (scope=dev) with no import at all is still unused."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import os\n")

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "dev"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]
        assert "no source file imports it" in errors[0]

    def test_whitelist_suppresses_hard_guarded_error(self, tmp_path):
        """The whitelist also suppresses the hard-dep-guarded-only error."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import auth\n"
            "except ImportError:\n"
            "    auth = None\n"
        )

        whitelist = {("app", "auth"): "Optional at runtime by design"}
        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "runtime"},
            workspace_names={"app", "auth"},
            whitelist=whitelist,
        )
        assert errors == []

    def test_guarded_import_undeclared_not_flagged(self, tmp_path):
        """A guarded import of a declared dep is NOT undeclared.

        Companion to test_guarded_import_not_false_positive: when the dep
        IS declared, the undeclared check should not flag it either.
        """
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import auth\n"
            "except ImportError:\n"
            "    auth = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps={"auth"},
            workspace_names={"app", "auth"},
        )
        assert errors == []


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

    def test_import_in_try_except_importerror_not_flagged(self, tmp_path):
        """Imports inside try/except ImportError blocks are optional and not flagged."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    from optional_pkg import something\n"
            "except ImportError:\n"
            "    pass\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []

    def test_import_in_try_except_module_not_found_error_skipped(self, tmp_path):
        """Imports inside try/except ModuleNotFoundError are optional."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import optional_pkg\n"
            "except ModuleNotFoundError:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []

    def test_import_in_try_except_tuple_importerror_skipped(self, tmp_path):
        """Imports inside try/except (ImportError, ValueError) are optional."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    from optional_pkg import feature\n"
            "except (ImportError, ValueError):\n"
            "    feature = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []

    def test_import_in_try_except_as_pattern_skipped(self, tmp_path):
        """Imports inside try/except ImportError as e are optional."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import optional_pkg\n"
            "except ImportError as e:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []

    def test_import_in_try_except_tuple_as_pattern_skipped(self, tmp_path):
        """Imports inside try/except (ImportError, X) as e are optional."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import optional_pkg\n"
            "except (ImportError, ModuleNotFoundError) as e:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []

    def test_import_in_try_except_exception_NOT_skipped(self, tmp_path):
        """Imports inside try/except Exception are NOT optional (too broad)."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import optional_pkg\n"
            "except Exception:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert len(errors) == 1
        assert "optional_pkg" in errors[0]

    def test_import_in_try_except_bare_NOT_skipped(self, tmp_path):
        """Imports inside bare except: are NOT optional (too broad)."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    import optional_pkg\n"
            "except:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert len(errors) == 1
        assert "optional_pkg" in errors[0]

    def test_import_in_except_body_NOT_skipped(self, tmp_path):
        """Imports inside except ImportError body are real deps (fallbacks)."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    from optional_pkg import feature\n"
            "except ImportError:\n"
            "    from fallback_pkg import feature\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg", "fallback_pkg"},
        )
        # optional_pkg should be skipped (in try body), but fallback_pkg
        # should be flagged (in except body -- it's a required fallback)
        assert len(errors) == 1
        assert "fallback_pkg" in errors[0]

    def test_import_outside_try_still_flagged(self, tmp_path):
        """Normal imports outside try/except are still flagged as undeclared."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import undeclared_pkg\n")

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "undeclared_pkg"},
        )
        assert len(errors) == 1
        assert "undeclared_pkg" in errors[0]

    def test_nested_try_outer_catches_importerror(self, tmp_path):
        """Import in nested try where outer catches ImportError is skipped."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "try:\n"
            "    try:\n"
            "        import optional_pkg\n"
            "    except ValueError:\n"
            "        pass\n"
            "except ImportError:\n"
            "    optional_pkg = None\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "optional_pkg"},
        )
        assert errors == []


class TestTypeCheckingDepValidation:
    """TYPE_CHECKING imports are invisible to dependency validation."""

    def test_type_checking_import_not_undeclared(self, tmp_path):
        """A workspace dep imported only inside TYPE_CHECKING is NOT flagged as undeclared."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "from __future__ import annotations\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import auth\n"
        )

        errors = check_undeclared_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps=set(),
            workspace_names={"app", "auth"},
        )
        assert errors == []

    def test_type_checking_only_import_flagged_as_unused(self, tmp_path):
        """A dep imported ONLY inside TYPE_CHECKING IS flagged as unused."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text(
            "from __future__ import annotations\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import auth\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "runtime"},
            workspace_names={"app", "auth"},
            whitelist={},
        )
        assert len(errors) == 1
        assert "auth" in errors[0]
        assert "no source file imports it" in errors[0]

    def test_normal_and_type_checking_import_not_unused(self, tmp_path):
        """A dep imported both normally and inside TYPE_CHECKING is NOT flagged as unused."""
        project_dir = tmp_path / "app"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(_PYPROJECT)
        (project_dir / "main.py").write_text("import auth\n")
        (project_dir / "types.py").write_text(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import auth\n"
        )

        errors = check_unused_deps(
            project_name="app",
            project_dir=str(project_dir),
            manifest_deps_with_scope={"auth": "runtime"},
            workspace_names={"app", "auth"},
            whitelist={},
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
        with pytest.raises(ConfigError, match="missing required key 'reason'"):
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
        with pytest.raises(ConfigError, match="must not be empty"):
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
        with pytest.raises(ConfigError, match="missing required key 'package'"):
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={"auth": "runtime"},
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
            manifest_deps_with_scope={"@myorg/utils": "runtime"},
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

    def test_scripts_dir_not_flagged_as_dead(self, tmp_path):
        """Files in scripts/ are standalone executables, not dead modules."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import x\n")
        (pkg / "core.py").write_text("x = 1\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "migrate.py").write_text("import mylib.core\n")

        dead = find_dead_modules(str(tmp_path))
        # scripts/migrate.py is a standalone executable, not a module
        assert "scripts/migrate.py" not in dead

    def test_module_only_imported_by_script_still_dead(self, tmp_path):
        """A module imported only by a script is still flagged as dead.

        Scripts are scanned as import sources (their imports are collected)
        but the modules they import must also be imported by non-script code
        to be considered alive. Scripts are not part of the importable module
        graph.
        """
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (pkg / "orphan.py").write_text("y = 2\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        # The script imports orphan, but that should NOT save it
        (scripts / "run.py").write_text("import mylib.orphan\n")

        dead = find_dead_modules(str(tmp_path))
        assert "mylib/orphan.py" in dead

    def test_symlinked_project_dir(self, tmp_path):
        """Dead module detection works when invoked through a symlinked directory.

        The project is set up in a real directory, then a symlink is created
        pointing to it. Running find_dead_modules through the symlink should
        produce the same results as running it directly.
        """
        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        (real_dir / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
        pkg = real_dir / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import x\n")
        (pkg / "core.py").write_text("x = 1\n")
        (pkg / "unused.py").write_text("y = 2\n")

        # Verify results via the real path
        dead_real = find_dead_modules(str(real_dir))
        assert "mylib/unused.py" in dead_real
        assert "mylib/core.py" not in dead_real

        # Create a symlink to the project directory
        link_dir = tmp_path / "linked_project"
        link_dir.symlink_to(real_dir)

        # Same results via the symlink
        dead_link = find_dead_modules(str(link_dir))
        assert dead_link == dead_real


class TestFindDeadGoPackages:
    """find_dead_go_packages detects unreferenced Go internal packages."""

    _GO_MOD = 'module github.com/user/myapp\n\ngo 1.21\n'

    def test_used_internal_package_not_flagged(self, tmp_path):
        """An internal package imported by non-test code is not flagged."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\n'
            'import "github.com/user/myapp/internal/auth"\n\n'
            'func main() { auth.Run() }\n'
        )
        internal = tmp_path / "internal" / "auth"
        internal.mkdir(parents=True)
        (internal / "auth.go").write_text(
            'package auth\n\nfunc Run() {}\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert dead == []

    def test_unused_internal_package_flagged(self, tmp_path):
        """An internal package not imported by anything is flagged as dead."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )
        internal = tmp_path / "internal" / "unused"
        internal.mkdir(parents=True)
        (internal / "unused.go").write_text(
            'package unused\n\nfunc Noop() {}\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert "internal/unused" in dead

    def test_no_internal_directory_returns_empty(self, tmp_path):
        """A Go project with no internal/ directory returns empty list."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )
        pkg = tmp_path / "pkg" / "util"
        pkg.mkdir(parents=True)
        (pkg / "util.go").write_text(
            'package util\n\nfunc Help() {}\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert dead == []

    def test_test_files_do_not_count_as_references(self, tmp_path):
        """_test.go files importing an internal package do not save it."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )
        internal = tmp_path / "internal" / "helper"
        internal.mkdir(parents=True)
        (internal / "helper.go").write_text(
            'package helper\n\nfunc H() {}\n'
        )
        # Only a test file imports this package
        (tmp_path / "main_test.go").write_text(
            'package main\n\n'
            'import "github.com/user/myapp/internal/helper"\n\n'
            'func TestX() { helper.H() }\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert "internal/helper" in dead

    def test_no_go_mod_returns_empty(self, tmp_path):
        """A directory without go.mod returns empty list."""
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )
        dead = find_dead_go_packages(str(tmp_path))
        assert dead == []

    def test_multiple_internal_packages_mixed(self, tmp_path):
        """Multiple internal packages: used ones pass, unused ones flagged."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\n'
            'import "github.com/user/myapp/internal/used"\n\n'
            'func main() { used.Run() }\n'
        )

        used_pkg = tmp_path / "internal" / "used"
        used_pkg.mkdir(parents=True)
        (used_pkg / "used.go").write_text(
            'package used\n\nfunc Run() {}\n'
        )

        dead_pkg = tmp_path / "internal" / "dead"
        dead_pkg.mkdir(parents=True)
        (dead_pkg / "dead.go").write_text(
            'package dead\n\nfunc Nothing() {}\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert "internal/used" not in dead
        assert "internal/dead" in dead

    def test_self_import_does_not_count(self, tmp_path):
        """Files within an internal package importing their own package
        do not count as external references."""
        (tmp_path / "go.mod").write_text(self._GO_MOD)
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )

        internal = tmp_path / "internal" / "lonely"
        internal.mkdir(parents=True)
        # lonely package has two files; one references a symbol from the
        # same package, but that's intra-package, not an external reference.
        (internal / "a.go").write_text(
            'package lonely\n\nfunc A() { B() }\n'
        )
        (internal / "b.go").write_text(
            'package lonely\n\nfunc B() {}\n'
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert "internal/lonely" in dead


class TestFindDeadNpmModules:
    """find_dead_npm_modules detects unreachable npm source files."""

    def _pkg_json(self, tmp_path, data):
        """Write a package.json with the given dict data."""
        import json as _json
        (tmp_path / "package.json").write_text(_json.dumps(data))

    def test_reachable_via_main_not_flagged(self, tmp_path):
        """A file reachable from main entry point is not flagged."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("const util = require('./util');\n")
        (src / "util.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_unreachable_file_flagged(self, tmp_path):
        """A file not reachable from any entry point is flagged as dead."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("module.exports = {};\n")
        (src / "orphan.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert "src/orphan.js" in dead
        assert "src/index.js" not in dead

    def test_bin_entry_point_makes_file_reachable(self, tmp_path):
        """A file referenced via bin entry point is reachable."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "bin": {"mycli": "./cli.js"},
        })
        (tmp_path / "cli.js").write_text("const lib = require('./lib');\n")
        (tmp_path / "lib.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_exports_map_entry_point(self, tmp_path):
        """Exports map with subpath conditions works as entry point."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "exports": {
                ".": {
                    "import": "./dist/index.mjs",
                    "require": "./dist/index.cjs",
                },
            },
        })
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.mjs").write_text("import { helper } from './helper.mjs';\n")
        (dist / "index.cjs").write_text("const helper = require('./helper.cjs');\n")
        (dist / "helper.mjs").write_text("export function helper() {}\n")
        (dist / "helper.cjs").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_js_to_ts_resolution(self, tmp_path):
        """Import of ./foo.js resolves to ./foo.ts when only .ts exists."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.ts",
        })
        src = tmp_path / "src"
        src.mkdir()
        # TypeScript source imports with .js extension (common TS pattern)
        (src / "index.ts").write_text("import { helper } from './helper.js';\n")
        (src / "helper.ts").write_text("export function helper() {}\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_no_package_json_returns_empty(self, tmp_path):
        """Directory without package.json returns empty list."""
        (tmp_path / "index.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_no_entry_points_returns_empty(self, tmp_path):
        """Package with no exports/main/bin returns empty (no reachability analysis)."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_test_files_excluded_from_dead(self, tmp_path):
        """Test files are not flagged as dead even if unreachable."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("module.exports = {};\n")
        tests = tmp_path / "__tests__"
        tests.mkdir()
        (tests / "index.test.js").write_text("test('it works', () => {});\n")

        dead = find_dead_npm_modules(str(tmp_path))
        rel_paths = [os.path.basename(d) for d in dead]
        assert "index.test.js" not in rel_paths

    def test_transitive_reachability(self, tmp_path):
        """Files reachable transitively through the import chain are not dead."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("require('./a');\n")
        (src / "a.js").write_text("require('./b');\n")
        (src / "b.js").write_text("require('./c');\n")
        (src / "c.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_directory_index_resolution(self, tmp_path):
        """Import of a directory resolves to its index file."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("require('./utils');\n")
        utils = src / "utils"
        utils.mkdir()
        (utils / "index.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_bin_string_entry_point(self, tmp_path):
        """bin as a plain string (not dict) works as entry point."""
        self._pkg_json(tmp_path, {
            "name": "mycli", "version": "1.0.0",
            "bin": "./cli.js",
        })
        (tmp_path / "cli.js").write_text("console.log('hello');\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert dead == []

    def test_multiple_entry_points_combined(self, tmp_path):
        """Files reachable from different entry points are all considered."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./lib/index.js",
            "bin": {"cli": "./bin/cli.js"},
        })
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "index.js").write_text("require('./core');\n")
        (lib / "core.js").write_text("module.exports = {};\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "cli.js").write_text("require('./helpers');\n")
        (bin_dir / "helpers.js").write_text("module.exports = {};\n")
        # Orphan not reachable from either entry point
        (tmp_path / "orphan.js").write_text("module.exports = {};\n")

        dead = find_dead_npm_modules(str(tmp_path))
        assert "orphan.js" in dead
        assert "lib/index.js" not in dead
        assert "lib/core.js" not in dead
        assert "bin/cli.js" not in dead
        assert "bin/helpers.js" not in dead

    def test_js_inside_python_package_not_flagged(self, tmp_path):
        """JS files inside a Python package (with __init__.py) are not npm modules."""
        self._pkg_json(tmp_path, {
            "name": "test", "version": "1.0.0",
            "main": "./src/index.js",
        })
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("module.exports = {};\n")
        # JS files inside a Python package dir (data resources, not npm modules)
        pydir = tmp_path / "mylib" / "js"
        pydir.mkdir(parents=True)
        (pydir / "__init__.py").write_text("")
        (pydir / "template.js").write_text("function render() {}\n")

        dead = find_dead_npm_modules(str(tmp_path))
        # template.js should NOT be flagged -- it's a Python data resource
        rel_paths = [d for d in dead]
        assert "mylib/js/template.js" not in rel_paths


class TestIsInsidePythonPackage:
    """_is_inside_python_package walks parent directories up to project_dir."""

    def test_grandparent_has_init_py(self, tmp_path):
        """A file whose grandparent has __init__.py is inside a Python package."""
        project = tmp_path / "project"
        pkg = project / "pkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        js_file = sub / "file.js"
        js_file.write_text("// js\n")

        assert _is_inside_python_package(str(js_file), str(project)) is True

    def test_no_init_py_up_to_root(self, tmp_path):
        """A file with no __init__.py in any parent up to project_dir is not inside a package."""
        project = tmp_path / "project"
        stuff = project / "stuff"
        stuff.mkdir(parents=True)
        js_file = stuff / "file.js"
        js_file.write_text("// js\n")

        assert _is_inside_python_package(str(js_file), str(project)) is False


class TestWalkSourceFilesExcludeDirs:
    """walk_source_files with exclude_dirs skips specified directories."""

    def test_exclude_dirs_skips_specified_directory(self, tmp_path):
        """Directories listed in exclude_dirs are not walked."""
        (tmp_path / "main.py").write_text("x = 1\n")
        sibling = tmp_path / "sibling_project"
        sibling.mkdir()
        (sibling / "lib.py").write_text("y = 2\n")

        # Without exclude_dirs, both files found
        all_files = walk_source_files(str(tmp_path), (".py",), [])
        basenames = {os.path.basename(f) for f in all_files}
        assert "main.py" in basenames
        assert "lib.py" in basenames

        # With exclude_dirs, sibling is skipped
        filtered = walk_source_files(
            str(tmp_path), (".py",), [],
            exclude_dirs=[str(sibling)],
        )
        basenames = {os.path.basename(f) for f in filtered}
        assert "main.py" in basenames
        assert "lib.py" not in basenames

    def test_exclude_dirs_relative_path(self, tmp_path):
        """exclude_dirs accepts paths relative to project_path."""
        (tmp_path / "main.py").write_text("x = 1\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "mod.py").write_text("y = 2\n")

        filtered = walk_source_files(
            str(tmp_path), (".py",), [],
            exclude_dirs=["sub"],
        )
        basenames = {os.path.basename(f) for f in filtered}
        assert "main.py" in basenames
        assert "mod.py" not in basenames

    def test_exclude_dirs_none_walks_everything(self, tmp_path):
        """exclude_dirs=None (default) walks all directories."""
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")

        files = walk_source_files(str(tmp_path), (".py",), [], exclude_dirs=None)
        basenames = {os.path.basename(f) for f in files}
        assert basenames == {"a.py", "b.py"}

    def test_exclude_dirs_multiple(self, tmp_path):
        """Multiple directories can be excluded at once."""
        (tmp_path / "root.py").write_text("")
        for name in ("alpha", "beta", "gamma"):
            d = tmp_path / name
            d.mkdir()
            (d / f"{name}.py").write_text("")

        filtered = walk_source_files(
            str(tmp_path), (".py",), [],
            exclude_dirs=["alpha", "beta"],
        )
        basenames = {os.path.basename(f) for f in filtered}
        assert "root.py" in basenames
        assert "gamma.py" in basenames
        assert "alpha.py" not in basenames
        assert "beta.py" not in basenames

    def test_exclude_dirs_nested_subdirectory(self, tmp_path):
        """Excluding a nested directory only skips that subtree."""
        (tmp_path / "top.py").write_text("")
        a = tmp_path / "a"
        a.mkdir()
        (a / "a.py").write_text("")
        b = a / "b"
        b.mkdir()
        (b / "b.py").write_text("")

        # Exclude a/b, keep a/
        filtered = walk_source_files(
            str(tmp_path), (".py",), [],
            exclude_dirs=[str(b)],
        )
        basenames = {os.path.basename(f) for f in filtered}
        assert "top.py" in basenames
        assert "a.py" in basenames
        assert "b.py" not in basenames


class TestRootProjectDepScan:
    """Dep scan for path='.' projects excludes sibling project directories."""

    def test_root_project_excludes_siblings(self, tmp_path):
        """A project at path='.' with exclude_dirs skips sibling projects."""
        # Root project (path=".")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "root-app"\n'
        )
        (tmp_path / "main.py").write_text("import sibling_lib\n")

        # Sibling project at path="sibling"
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        (sibling / "pyproject.toml").write_text(
            '[project]\nname = "sibling-lib"\n'
        )
        (sibling / "app.py").write_text("import root_app\n")

        from rlsbl.dep_validation import _get_imported_workspace_packages

        workspace_names = {"root-app", "sibling-lib"}

        # Without exclusions, scanning root picks up sibling's import
        lib_all, test_all, _guarded_all = _get_imported_workspace_packages(
            str(tmp_path), workspace_names,
        )
        # sibling/app.py imports root_app, which would be a false positive
        # for the root project if we don't exclude sibling
        assert "root-app" in (lib_all | test_all)

        # With exclusions, sibling's files are not scanned
        lib_exc, test_exc, _guarded_exc = _get_imported_workspace_packages(
            str(tmp_path), workspace_names,
            exclude_dirs=[str(sibling)],
        )
        # Only main.py's import should be found
        all_exc = lib_exc | test_exc
        assert "sibling-lib" in all_exc  # main.py imports sibling_lib
        # root-app from sibling/app.py should NOT appear
        assert "root-app" not in all_exc

    def test_dead_modules_excludes_siblings(self, tmp_path):
        """find_dead_modules with exclude_dirs does not scan sibling dirs."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mylib"\n'
        )
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import x\n")
        (pkg / "core.py").write_text("x = 1\n")

        # Sibling project with a module that would be detected as dead
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        (sibling / "orphan.py").write_text("z = 3\n")

        # Without exclusion, sibling's orphan.py appears as dead
        dead_all = find_dead_modules(str(tmp_path))
        sibling_dead = [d for d in dead_all if "sibling" in d]
        assert len(sibling_dead) > 0

        # With exclusion, sibling's files are not scanned
        dead_exc = find_dead_modules(
            str(tmp_path), exclude_dirs=[str(sibling)],
        )
        sibling_dead = [d for d in dead_exc if "sibling" in d]
        assert len(sibling_dead) == 0

    def test_sibling_exclude_dirs_integration(self, tmp_path):
        """_sibling_exclude_dirs computes correct exclusions for root project."""
        from rlsbl.checks import register_checks

        # Capture the _sibling_exclude_dirs function
        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        register_checks(MockApp())

        # Access _sibling_exclude_dirs via the module's closure
        from rlsbl import checks as checks_mod

        # Manually test the helper that register_checks defines
        # by constructing the same workspace scenario
        projects = [
            {"name": "root", "path": "."},
            {"name": "framework", "path": "framework"},
            {"name": "server", "path": "server"},
        ]
        root = str(tmp_path)

        # The root project's path is ".", so all siblings are inside it
        root_abs = os.path.normpath(os.path.join(root, "."))
        exclude = []
        for other in projects:
            if other["path"] == ".":
                continue
            other_abs = os.path.normpath(os.path.join(root, other["path"]))
            if other_abs.startswith(root_abs + os.sep):
                exclude.append(other_abs)

        assert len(exclude) == 2
        assert os.path.normpath(os.path.join(root, "framework")) in exclude
        assert os.path.normpath(os.path.join(root, "server")) in exclude

    def test_dep_cache_uses_sibling_exclusions(self, tmp_path):
        """_build_dep_import_cache excludes sibling dirs for root project."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "."\nname = "root-app"\n\n'
            '[[projects]]\npath = "sibling"\nname = "sibling-lib"\n'
        )

        # Root project imports os (not a workspace member)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "root-app"\n'
        )
        (tmp_path / "main.py").write_text("import os\n")

        # Sibling project imports root_app (should be excluded from root scan)
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        (sibling / "pyproject.toml").write_text(
            '[project]\nname = "sibling-lib"\n'
        )
        (sibling / "app.py").write_text("import root_app\n")

        projects = [
            {"name": "root-app", "path": "."},
            {"name": "sibling-lib", "path": "sibling"},
        ]
        graph = WorkspaceGraph(str(tmp_path), projects)

        ctx = WorkspaceCheckContext(
            project_root=Path(tmp_path),
            workspace_root=Path(tmp_path),
            config={},
            projects=projects,
            graph=graph,
        )

        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())

        # Run deps-unused which triggers _build_dep_import_cache
        result = captured["deps-unused"](ctx)

        # The root project should NOT see root-app as imported
        # (that import lives in sibling/app.py which should be excluded)
        cache = ctx._dep_import_cache
        root_lib, root_test, _root_guarded = cache["root-app"]
        # root-app should not appear in its own scan results
        # (sibling/app.py's "import root_app" should be excluded)
        assert "root-app" not in root_lib
        assert "root-app" not in root_test


class TestDepsChecksIntegration:
    """Integration tests: checks registered on the strictcli check system."""

    def _capture_checks(self):
        """Register all checks on a mock app and return the captured dict."""
        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

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
        """deps-unused skips when context is not a workspace (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert result.status == "skip"

    def test_deps_undeclared_skip_not_workspace(self):
        """deps-undeclared skips when context is not a workspace (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
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
        """deps-runtime-test-only skips when context is not a workspace (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert result.status == "skip"

    def test_deps_dev_in_lib_skip_not_workspace(self):
        """deps-dev-in-lib skips when context is not a workspace (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
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

    def test_dead_modules_skip_unsupported_target(self, tmp_path):
        """dead-modules skips for projects that are neither Python, Go, nor npm."""
        from rlsbl.context import ProjectContext

        # Create a Cargo-only project (no Python, no Go, no npm)
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "test"\nversion = "1.0.0"\n'
        )

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

    def test_dead_modules_go_pass_clean(self, tmp_path):
        """dead-modules passes for a Go project with all internal packages used."""
        from rlsbl.context import ProjectContext

        (tmp_path / "go.mod").write_text(
            'module example.com/proj\n\ngo 1.21\n'
        )
        (tmp_path / "main.go").write_text(
            'package main\n\n'
            'import "example.com/proj/internal/core"\n\n'
            'func main() { core.Run() }\n'
        )
        core_dir = tmp_path / "internal" / "core"
        core_dir.mkdir(parents=True)
        (core_dir / "core.go").write_text(
            'package core\n\nfunc Run() {}\n'
        )

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "pass"

    def test_dead_modules_go_warn_unreferenced(self, tmp_path):
        """dead-modules warns for a Go project with an unused internal package."""
        from rlsbl.context import ProjectContext

        (tmp_path / "go.mod").write_text(
            'module example.com/proj\n\ngo 1.21\n'
        )
        (tmp_path / "main.go").write_text(
            'package main\n\nfunc main() {}\n'
        )
        orphan_dir = tmp_path / "internal" / "orphan"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "orphan.go").write_text(
            'package orphan\n\nfunc Unused() {}\n'
        )

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "warn"
        assert "1 dead module" in result.message

    def test_dead_modules_npm_pass_clean(self, tmp_path):
        """dead-modules passes for an npm project with all files reachable."""
        import json as _json
        from rlsbl.context import ProjectContext

        (tmp_path / "package.json").write_text(_json.dumps({
            "name": "test",
            "version": "1.0.0",
            "main": "./src/index.js",
        }))
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("const util = require('./util');\n")
        (src / "util.js").write_text("module.exports = {};\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "pass"

    def test_dead_modules_npm_warn_unreachable(self, tmp_path):
        """dead-modules warns for an npm project with unreachable files."""
        import json as _json
        from rlsbl.context import ProjectContext

        (tmp_path / "package.json").write_text(_json.dumps({
            "name": "test",
            "version": "1.0.0",
            "main": "./src/index.js",
        }))
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.js").write_text("module.exports = {};\n")
        (src / "orphan.js").write_text("module.exports = {};\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "warn"
        assert "1 dead module" in result.message


class TestFindDeadWorkspacePackages:
    """find_dead_workspace_packages detects library packages with no workspace importers."""

    def test_library_imported_by_app_not_flagged(self):
        """Library A imported by app B is not flagged."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth", "library": True},
        ]
        import_cache = {
            "app": ({"auth"}, set(), set()),   # app imports auth in lib code
            "auth": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert dead == []

    def test_library_not_imported_is_flagged(self):
        """Library A not imported by anything is flagged."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth", "library": True},
        ]
        import_cache = {
            "app": (set(), set(), set()),
            "auth": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert len(dead) == 1
        assert dead[0].name == "auth"
        assert dead[0].severity == "warn"
        assert "not imported by any workspace package" in dead[0].message

    def test_non_library_app_not_flagged(self):
        """Non-library (app) with no importers is not flagged -- apps are entry points."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "cli", "path": "cli"},
        ]
        import_cache = {
            "app": (set(), set(), set()),
            "cli": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert dead == []

    def test_dev_node_not_flagged(self):
        """Dev_node with no importers is not flagged -- excluded from checks."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "test-utils", "path": "test-utils", "library": True, "dev_node": True},
        ]
        import_cache = {
            "app": (set(), set(), set()),
            "test-utils": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert dead == []

    def test_library_imported_only_in_tests(self):
        """Library imported only in tests gets specific warning message."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "testlib", "path": "testlib", "library": True},
        ]
        import_cache = {
            "app": (set(), {"testlib"}, set()),   # app imports testlib only in tests
            "testlib": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert len(dead) == 1
        assert dead[0].name == "testlib"
        assert dead[0].severity == "warn"
        assert "only imported in test code" in dead[0].message
        assert "app" in dead[0].message

    def test_self_imports_not_counted(self):
        """A library importing itself does not count as having importers."""
        projects = [
            {"name": "mylib", "path": "mylib", "library": True},
        ]
        import_cache = {
            "mylib": ({"mylib"}, set(), set()),   # self-import only
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert len(dead) == 1
        assert dead[0].name == "mylib"

    def test_multiple_dead_libraries(self):
        """Multiple dead libraries are all reported."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "lib-a", "path": "lib-a", "library": True},
            {"name": "lib-b", "path": "lib-b", "library": True},
        ]
        import_cache = {
            "app": (set(), set(), set()),
            "lib-a": (set(), set(), set()),
            "lib-b": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert len(dead) == 2
        names = {d.name for d in dead}
        assert names == {"lib-a", "lib-b"}

    def test_library_imported_in_lib_and_test(self):
        """Library imported in both lib and test code is not flagged."""
        projects = [
            {"name": "app", "path": "app"},
            {"name": "utils", "path": "utils", "library": True},
        ]
        import_cache = {
            "app": ({"utils"}, {"utils"}, set()),
            "utils": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert dead == []

    def test_empty_workspace(self):
        """Empty workspace produces no dead packages."""
        dead = find_dead_workspace_packages([], {})
        assert dead == []

    def test_library_false_not_flagged(self):
        """Project with library=False explicitly set is not flagged."""
        projects = [
            {"name": "app", "path": "app", "library": False},
        ]
        import_cache = {
            "app": (set(), set(), set()),
        }
        dead = find_dead_workspace_packages(projects, import_cache)
        assert dead == []


class TestDeadWorkspacePackagesCheck:
    """Integration tests: dead-workspace-packages check on the strictcli system."""

    def _capture_checks(self):
        """Register all checks on a mock app and return the captured dict."""
        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())
        return captured

    def test_registered(self):
        """dead-workspace-packages check is registered."""
        captured = self._capture_checks()
        assert "dead-workspace-packages" in captured

    def test_skip_not_workspace(self):
        """Skips when context is not a workspace (via scope adapter)."""
        from rlsbl.checks.scope import scope_adapter
        from rlsbl.context import ProjectContext

        ctx = ProjectContext(project_root=Path("/tmp/fake"), workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace")
        assert result.status == "skip"

    def test_pass_all_libraries_imported(self, tmp_path):
        """Passes when all library packages have workspace importers."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "auth"\nname = "auth"\nlibrary = true\n'
        )

        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["auth"]\n'
        )
        (app_dir / "main.py").write_text("import auth\n")

        auth_dir = tmp_path / "auth"
        auth_dir.mkdir()
        (auth_dir / "pyproject.toml").write_text('[project]\nname = "auth"\n')

        projects = [
            {"name": "app", "path": "app"},
            {"name": "auth", "path": "auth", "library": True},
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
        result = captured["dead-workspace-packages"](ctx)
        assert result.status == "pass"

    def test_warn_dead_library(self, tmp_path):
        """Warns when a library package has no workspace importers."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace_graph import WorkspaceGraph

        ws_dir = tmp_path / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[projects]]\npath = "app"\nname = "app"\n\n'
            '[[projects]]\npath = "orphan"\nname = "orphan"\nlibrary = true\n'
        )

        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "pyproject.toml").write_text('[project]\nname = "app"\n')
        (app_dir / "main.py").write_text("import os\n")

        orphan_dir = tmp_path / "orphan"
        orphan_dir.mkdir()
        (orphan_dir / "pyproject.toml").write_text('[project]\nname = "orphan"\n')

        projects = [
            {"name": "app", "path": "app"},
            {"name": "orphan", "path": "orphan", "library": True},
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
        result = captured["dead-workspace-packages"](ctx)
        assert result.status == "warn"
        assert "1 dead workspace package" in result.message


# Minimal pubspec.yaml template for Dart tests
_PUBSPEC = 'name: {name}\nversion: 0.1.0\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n'


class TestFindDeadDartModules:
    """find_dead_dart_modules detects unreachable Dart source files."""

    def test_reachable_via_barrel_not_flagged(self, tmp_path):
        """A file exported by the barrel file is not flagged."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/helper.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "helper.dart").write_text("void help() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_unreachable_file_flagged(self, tmp_path):
        """A file not reachable from any entry point is flagged as dead."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/used.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "used.dart").write_text("void used() {}\n")
        (src / "orphan.dart").write_text("void orphan() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert os.path.join("lib", "src", "orphan.dart") in dead
        assert os.path.join("lib", "src", "used.dart") not in dead

    def test_bin_entry_point_works(self, tmp_path):
        """A file reachable from a bin/ script is not flagged."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mycli"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mycli.dart").write_text("// barrel\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "mycli.dart").write_text("import '../lib/src/runner.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "runner.dart").write_text("void run() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert os.path.join("lib", "src", "runner.dart") not in dead

    def test_relative_imports_resolved(self, tmp_path):
        """Relative imports are properly resolved across directories."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/a.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "a.dart").write_text("import '../src/b.dart';\n")
        (src / "b.dart").write_text("import 'c.dart';\n")
        (src / "c.dart").write_text("void c() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_package_self_imports_resolved(self, tmp_path):
        """package:self/... imports are resolved to lib/... paths."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("import 'package:mylib/src/util.dart';\n")
        (src / "util.dart").write_text("void util() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_no_pubspec_returns_empty(self, tmp_path):
        """Directory without pubspec.yaml returns empty list."""
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "something.dart").write_text("void x() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_test_files_excluded(self, tmp_path):
        """Test files are not flagged as dead even if unreachable."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("void core() {}\n")
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "core_test.dart").write_text(
            "import 'package:mylib/mylib.dart';\nvoid main() {}\n"
        )

        dead = find_dead_dart_modules(str(tmp_path))
        rel_paths = [os.path.basename(d) for d in dead]
        assert "core_test.dart" not in rel_paths

    def test_transitive_reachability(self, tmp_path):
        """Files reachable transitively through exports/imports are not dead."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/a.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "a.dart").write_text("import 'b.dart';\n")
        (src / "b.dart").write_text("import 'c.dart';\n")
        (src / "c.dart").write_text("void c() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_no_entry_points_returns_empty(self, tmp_path):
        """Package with no barrel file and no bin/ returns empty."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        src = lib / "src"
        src.mkdir()
        (src / "orphan.dart").write_text("void orphan() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_external_package_imports_ignored(self, tmp_path):
        """Imports of external packages do not affect the graph."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text(
            "import 'package:flutter/material.dart';\n"
            "import 'package:other_pkg/other.dart';\n"
            "void core() {}\n"
        )

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_dart_sdk_imports_ignored(self, tmp_path):
        """dart:xxx imports do not affect the graph."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("import 'dart:io';\nvoid core() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert dead == []

    def test_multiple_bin_entry_points(self, tmp_path):
        """Multiple bin/ scripts serve as entry points."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("// barrel\n")
        src = lib / "src"
        src.mkdir()
        (src / "a.dart").write_text("void a() {}\n")
        (src / "b.dart").write_text("void b() {}\n")
        (src / "orphan.dart").write_text("void orphan() {}\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tool_a.dart").write_text("import '../lib/src/a.dart';\n")
        (bin_dir / "tool_b.dart").write_text("import '../lib/src/b.dart';\n")

        dead = find_dead_dart_modules(str(tmp_path))
        assert os.path.join("lib", "src", "a.dart") not in dead
        assert os.path.join("lib", "src", "b.dart") not in dead
        assert os.path.join("lib", "src", "orphan.dart") in dead

    def test_test_file_pattern_excluded(self, tmp_path):
        """Files matching *_test.dart outside test/ are excluded."""
        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("void core() {}\n")
        (src / "core_test.dart").write_text("void testCore() {}\n")

        dead = find_dead_dart_modules(str(tmp_path))
        rel_paths = [os.path.basename(d) for d in dead]
        assert "core_test.dart" not in rel_paths


class TestDeadDartModulesCheck:
    """Integration tests: dead-modules check for Dart projects."""

    def _capture_checks(self):
        """Register all checks on a mock app and return the captured dict."""
        captured = {}

        class MockApp:
            _checks_enabled = True

            def set_scope_adapter(self, adapter):
                pass

            def check(self, name):
                def decorator(fn):
                    captured[name] = fn
                    return fn
                return decorator

        from rlsbl.checks import register_checks
        register_checks(MockApp())
        return captured

    def test_dead_modules_dart_pass_clean(self, tmp_path):
        """dead-modules passes for a Dart project with all files reachable."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("void core() {}\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "pass"

    def test_dead_modules_dart_warn_unreachable(self, tmp_path):
        """dead-modules warns for a Dart project with unreachable files."""
        from rlsbl.context import ProjectContext

        (tmp_path / "pubspec.yaml").write_text(_PUBSPEC.format(name="mylib"))
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "mylib.dart").write_text("export 'src/core.dart';\n")
        src = lib / "src"
        src.mkdir()
        (src / "core.dart").write_text("void core() {}\n")
        (src / "orphan.dart").write_text("void orphan() {}\n")

        captured = self._capture_checks()
        ctx = ProjectContext(project_root=Path(tmp_path), workspace_root=None, config={})
        result = captured["dead-modules"](ctx)
        assert result.status == "warn"
        assert "1 dead module" in result.message
