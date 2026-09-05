"""Tests for Go linting -- library boundary violations in Go source files."""

import pytest

from rlsbl.lint import lint_library
from rlsbl.lint.config import LanguageLintConfig
from rlsbl.lint.go_ast import GoAstLinter
from rlsbl.lint.go_regex import GoRegexLinter


def _make_go_project(tmp_path, go_source, go_filename="lib.go"):
    """Create a minimal Go project with a go.mod and source file."""
    (tmp_path / "go.mod").write_text("module example.com/mylib\n\ngo 1.21\n")
    (tmp_path / go_filename).write_text(go_source)


def _default_config(**overrides):
    """Create a default Go lint config."""
    kwargs = {
        "forbidden_imports": ["net/http", "github.com/spf13/cobra", "github.com/urfave/cli"],
    }
    kwargs.update(overrides)
    return LanguageLintConfig(**kwargs)


@pytest.fixture(params=[GoAstLinter, GoRegexLinter], ids=["ast", "regex"])
def linter(request):
    return request.param()


class TestForbiddenImport:
    """Detect forbidden Go package imports."""

    def test_single_import_forbidden(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "forbidden-import"
        assert r.severity == "error"
        assert "net/http" in r.message

    def test_grouped_import_forbidden(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport (\n\t"fmt"\n\t"net/http"\n)\n')
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "forbidden-import"
        assert "net/http" in r.message

    def test_allowed_import(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n')
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_multiple_forbidden(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport (\n\t"net/http"\n\t"github.com/spf13/cobra"\n)\n')
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 2
        pkgs = {r.message.split("'")[1] for r in results}
        assert pkgs == {"net/http", "github.com/spf13/cobra"}

    def test_integration_via_lint_library(self, tmp_path):
        """lint_library() detects Go projects via go.mod."""
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        results = lint_library(str(tmp_path))
        forbidden = [r for r in results if r.rule == "forbidden-import"]
        assert len(forbidden) == 1
        assert "net/http" in forbidden[0].message


class TestStdoutDetection:
    """Detect fmt.Print* and os.Stdout.Write calls."""

    def test_fmt_println(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n\nfunc Do() {\n\tfmt.Println("hello")\n}\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert r.severity == "error"
        assert "fmt.Println()" in r.message

    def test_fmt_printf(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n\nfunc Do() {\n\tfmt.Printf("%s", "x")\n}\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "fmt.Printf()" in results[0].message

    def test_fmt_print(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n\nfunc Do() {\n\tfmt.Print("hi")\n}\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        assert "fmt.Print()" in results[0].message

    def test_os_stdout_write(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "os"\n\nfunc Do() {\n\tos.Stdout.Write([]byte("hello"))\n}\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "stdout"
        assert "os.Stdout" in r.message

    def test_stdout_disabled(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n\nfunc Do() {\n\tfmt.Println("hello")\n}\n')
        config = _default_config(forbidden_imports=[], stdout_enabled=False)
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_stdout_ignore_fmt(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "fmt"\n\nfunc Do() {\n\tfmt.Println("hello")\n}\n')
        config = _default_config(forbidden_imports=[], stdout_ignore=["fmt"])
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_stdout_ignore_os(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "os"\n\nfunc Do() {\n\tos.Stdout.Write([]byte("hello"))\n}\n')
        config = _default_config(forbidden_imports=[], stdout_ignore=["os"])
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestEntryPoint:
    """Detect func main() in package main files."""

    def test_func_main_detected(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package main\n\nfunc main() {\n}\n', "main.go")
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert len(results) == 1
        r = results[0]
        assert r.rule == "entry-point"
        assert r.severity == "error"
        assert "func main()" in r.message

    def test_no_entry_point_in_library(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package mylib\n\nfunc DoStuff() {\n}\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_entry_point_disabled(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package main\n\nfunc main() {\n}\n', "main.go")
        config = _default_config(forbidden_imports=[], entry_point_enabled=False)
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_entry_point_ignore(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package main\n\nfunc main() {\n}\n', "main.go")
        config = _default_config(forbidden_imports=[], entry_point_ignore=["main"])
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestCleanProject:
    """A Go project with no violations returns an empty list."""

    def test_clean(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package mylib\n\nimport "strings"\n\nfunc Upper(s string) string {\n\treturn strings.ToUpper(s)\n}\n')
        config = _default_config()
        results = linter.lint(str(tmp_path), config)
        assert results == []


class TestConfigSuppression:
    """Config-based suppression of rules."""

    def test_custom_forbidden_imports(self, tmp_path, linter):
        """Only the specified imports are forbidden."""
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        config = _default_config(forbidden_imports=["github.com/spf13/cobra"])
        results = linter.lint(str(tmp_path), config)
        assert results == []

    def test_empty_forbidden_imports(self, tmp_path, linter):
        _make_go_project(tmp_path, 'package lib\n\nimport "net/http"\n')
        config = _default_config(forbidden_imports=[])
        results = linter.lint(str(tmp_path), config)
        assert results == []
