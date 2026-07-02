"""Tests for GoTarget project-level detection via the go_introspect helper.

Covers the layouts that hand-rolled main.go globbing misdetected:
- cmd/x/cli.go entry files (previously classified as a library)
- broken-root layouts (root package main with no func main), which
  previously produced a silent goreleaserMain fallback to "."
- version.go scaffolding into the detected main-package dir
- dev install commands driven by declared install_paths
"""

import json

import pytest

from conftest import make_ctx
from rlsbl.go_introspect import GoIntrospectError
from rlsbl.targets.go import GoTarget


GO_MOD = "module github.com/user/myapp\n\ngo 1.21\n"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_config(root, install_paths=None):
    entry = {"type": "go", "local": True}
    if install_paths is not None:
        entry["install_paths"] = install_paths
    config = {"pipelines": {"go": entry}, "private": False}
    _write(root / ".rlsbl" / "config.json", json.dumps(config))
    return config


class TestCliGoLayoutDetection:
    """cmd/x/cli.go (entry file not named main.go) is a binary project."""

    def test_is_library_false_for_cli_go_layout(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        assert target._is_library(str(tmp_path)) is False

    def test_has_cmd_main_true_for_cli_go_layout(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        assert target._has_cmd_main(str(tmp_path)) is True

    def test_goreleaser_main_for_cli_go_layout(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "VERSION", "0.1.0\n")
        vars_ = target.template_vars(str(tmp_path), make_ctx(tmp_path))
        assert vars_["goreleaserMain"] == "./cmd/myapp"

    def test_scaffold_version_go_into_cmd_dir(self, tmp_path):
        """version.go is scaffolded into the detected main-package dir,
        never into the project root of a cmd-layout project (a root
        package main with no func main breaks `go build ./...`)."""
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        mappings = target.template_mappings(make_ctx(tmp_path))
        targets = [m["target"] for m in mappings]
        assert "cmd/myapp/version.go" in targets
        assert "version.go" not in targets

    def test_has_version_var_looks_in_main_package_dir(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "cmd" / "myapp" / "version.go", "package main\n\nvar Version string\n")
        mappings = target.template_mappings(make_ctx(tmp_path))
        targets = [m["target"] for m in mappings]
        assert not any(t.endswith("version.go") for t in targets)


class TestBrokenRootLayout:
    """Root package main with no func main + real main under cmd/."""

    def _broken_root(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "version.go", "package main\n\nvar Version string\n")
        _write(tmp_path / "cmd" / "myapp" / "main.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "VERSION", "0.1.0\n")

    def test_goreleaser_main_hard_errors_without_declaration(self, tmp_path):
        """Ambiguous mains must never silently fall back to '.'."""
        target = GoTarget()
        self._broken_root(tmp_path)
        with pytest.raises(GoIntrospectError, match="install_paths"):
            target.template_vars(str(tmp_path), make_ctx(tmp_path))

    def test_goreleaser_main_uses_declared_install_path(self, tmp_path):
        target = GoTarget()
        self._broken_root(tmp_path)
        config = _write_config(tmp_path, install_paths=["./cmd/myapp"])
        vars_ = target.template_vars(str(tmp_path), make_ctx(tmp_path, config))
        assert vars_["goreleaserMain"] == "./cmd/myapp"


class TestDevInstallCommand:
    def test_uses_declared_install_paths(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        _write_config(tmp_path, install_paths=["./cmd/myapp"])
        spec = target.dev_install_command(str(tmp_path))
        assert spec["global"]["args"] == ["install", "./cmd/myapp"]

    def test_library_returns_noop_spec(self, tmp_path, capsys):
        """A Go library (zero main packages) has nothing to `go install`:
        dev_install_command returns the no-op spec shape that `rlsbl dev
        install` skips, instead of demanding an install_paths declaration
        that no config could satisfy (any declared path would fail
        validation because there are no main packages)."""
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "lib.go", "package myapp\n\nfunc Hello() {}\n")
        _write_config(tmp_path)  # go pipeline without install_paths
        spec = target.dev_install_command(str(tmp_path))
        assert spec == {"global": None, "venv": None}
        assert "nothing to install" in capsys.readouterr().out

    def test_undeclared_install_paths_is_hard_error(self, tmp_path):
        """A Go project without declared install_paths cannot be
        dev-installed -- the error names the key and shows detected mains."""
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        _write_config(tmp_path)  # go pipeline without install_paths
        with pytest.raises(GoIntrospectError) as exc:
            target.dev_install_command(str(tmp_path))
        assert "install_paths" in str(exc.value)
        assert "./cmd/myapp" in str(exc.value)

    def test_non_go_dir_returns_placeholder_for_docs(self, tmp_path):
        """Doc introspection calls dev_install_command outside Go projects;
        it must return a generic spec, not raise."""
        target = GoTarget()
        spec = target.dev_install_command(str(tmp_path))
        assert spec["global"]["tool"] == "go"
        assert spec["global"]["args"][0] == "install"


class TestGoreleaserLdflags:
    def test_template_injects_exported_version_var(self):
        """goreleaser ldflags must set main.Version (matching `var Version`
        in version.go.tpl); lowercase main.version was a silent no-op."""
        import os
        tpl = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rlsbl", "templates", "go", "goreleaser.yml.tpl",
        )
        content = open(tpl).read()
        assert "-X main.Version={{.Version}}" in content
        assert "main.version=" not in content
