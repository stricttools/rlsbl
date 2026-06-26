"""Tests for per-package lint exception support (allowed_imports).

Covers workspace-level lint_allow, per-project TOML allow lists,
their union behavior, config isolation between calls, and no-op
cases where the allowed import is not in the forbidden list.
"""

from rlsbl.lint import lint_library


def _make_go_project(tmp_path, go_source, go_filename="lib.go"):
    """Create a minimal Go project with a go.mod and source file."""
    (tmp_path / "go.mod").write_text("module example.com/mylib\n\ngo 1.21\n")
    (tmp_path / go_filename).write_text(go_source)


def _make_python_project(tmp_path, py_source, py_filename="lib.py"):
    """Create a minimal Python project with a pyproject.toml and source file."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\n')
    (tmp_path / py_filename).write_text(py_source)


class TestWorkspaceLintAllow:
    """Workspace-level lint_allow passed via allowed_imports parameter."""

    def test_go_allowed_import_passes(self, tmp_path):
        """Go project with lint_allow=['net/http'] does not flag net/http."""
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        results = lint_library(str(tmp_path), allowed_imports=["net/http"])
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert forbidden == []

    def test_go_without_lint_allow_flags(self, tmp_path):
        """Same Go project without lint_allow flags net/http as forbidden."""
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1
        assert "net/http" in forbidden[0].message


class TestPerProjectTomlAllow:
    """Per-project TOML [forbidden-imports] allow list."""

    def test_python_toml_allow_passes(self, tmp_path):
        """Python project with TOML allow=['fastapi'] does not flag fastapi."""
        _make_python_project(tmp_path, "import fastapi\n")
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        (lint_dir / "python.toml").write_text(
            '[forbidden-imports]\nallow = ["fastapi"]\n'
        )
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert forbidden == []

    def test_python_without_toml_allow_flags(self, tmp_path):
        """Same Python project without TOML allow flags fastapi."""
        _make_python_project(tmp_path, "import fastapi\n")
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1
        assert "fastapi" in forbidden[0].message


class TestMergedAllowLists:
    """Workspace lint_allow and per-project TOML allow merge (union)."""

    def test_both_allow_lists_merge(self, tmp_path):
        """Union of workspace and TOML allow lists exempts both imports."""
        _make_python_project(
            tmp_path, "import fastapi\nimport click\n"
        )
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        # TOML allows fastapi
        (lint_dir / "python.toml").write_text(
            '[forbidden-imports]\nallow = ["fastapi"]\n'
        )
        # Workspace allows click
        results = lint_library(str(tmp_path), allowed_imports=["click"])
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert forbidden == []

    def test_partial_merge(self, tmp_path):
        """Only one of two forbidden imports is allowed -- the other is flagged."""
        _make_python_project(
            tmp_path, "import fastapi\nimport click\n"
        )
        lint_dir = tmp_path / ".rlsbl" / "lint"
        lint_dir.mkdir(parents=True)
        # TOML allows fastapi only
        (lint_dir / "python.toml").write_text(
            '[forbidden-imports]\nallow = ["fastapi"]\n'
        )
        # No workspace allow for click
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1
        assert "click" in forbidden[0].message


class TestConfigIsolation:
    """Allowed imports for one call must not persist to the next."""

    def test_isolation_between_calls(self, tmp_path):
        """First call with allowed_imports, second without -- second flags."""
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')

        # First call: net/http allowed
        results1 = lint_library(str(tmp_path), allowed_imports=["net/http"])
        forbidden1 = [r for r in results1 if r.rule == "forbidden-import"]
        assert forbidden1 == []

        # Second call: no allowed_imports -- net/http should be flagged again
        results2 = lint_library(str(tmp_path))
        forbidden2 = [r for r in results2 if r.rule == "forbidden-import"]
        assert len(forbidden2) == 1
        assert "net/http" in forbidden2[0].message


class TestAllowNonForbidden:
    """Allowing an import that's not in the forbidden list is a no-op."""

    def test_allow_non_forbidden_no_effect(self, tmp_path):
        """lint_allow with an import not in forbidden list causes no error."""
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n')
        results = lint_library(str(tmp_path), allowed_imports=["some/random/pkg"])
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert forbidden == []
