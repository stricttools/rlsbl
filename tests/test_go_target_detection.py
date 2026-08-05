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
    config = {"pipelines": {"go": entry}, "publish_mode": "ci"}
    _write(root / ".rlsbl" / "config.json", json.dumps(config))
    return config


class TestCliGoLayoutDetection:
    """cmd/x/cli.go (entry file not named main.go) is a binary project."""

    def test_is_library_false_for_cli_go_layout(self, tmp_path):
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        assert target._is_library(str(tmp_path)) is False

    def test_main_package_enumerated_for_cli_go_layout(self, tmp_path):
        """Re-targeted from the deleted _has_cmd_main wrapper: the helper
        must enumerate the cmd/ main even when its entry file is cli.go."""
        from rlsbl.go_introspect import list_main_packages
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "myapp" / "cli.go", "package main\n\nfunc main() {}\n")
        mains = list_main_packages(str(tmp_path))
        assert [p.rel_dir for p in mains] == ["./cmd/myapp"]

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
        validation because there are no main packages). The "reason" key
        carries the explanation for the skip message; nothing is printed
        here (dev install owns the output)."""
        target = GoTarget()
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "lib.go", "package myapp\n\nfunc Hello() {}\n")
        _write_config(tmp_path)  # go pipeline without install_paths
        spec = target.dev_install_command(str(tmp_path))
        assert spec == {
            "global": None,
            "venv": None,
            "reason": "Go library: nothing to install (no main packages)",
        }
        assert capsys.readouterr().out == ""

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
        # The suggestion advertises pasting into .rlsbl/config.json, so it
        # must be valid JSON (double quotes), not a Python list repr.
        assert '["./cmd/myapp"]' in str(exc.value)

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


class TestDeclaredArtifactIsAuthoritative:
    """A go pipeline's declared ``artifact`` wins over filesystem introspection.

    Regression: scaffold classified a Go project by scanning for ``package main``
    anywhere in the tree, so a project that DECLARES ``artifact: "library"`` but
    ships a dev helper under ``scripts/`` was scaffolded as a binary --
    ``.goreleaser.yml`` pointed at the helper and a ``version.go`` was written
    into it, while the publish workflow (which does honour the declaration)
    correctly ran no goreleaser job. The declaration is the operator's committed
    choice; detection only ever feeds the missing-key error message.
    """

    @staticmethod
    def _library_project_with_dev_helper(root):
        _write(root / "go.mod", GO_MOD)
        _write(root / "lib.go", "package myapp\n\nfunc Hello() {}\n")
        # A dev helper -- a real `package main`, but not a published binary.
        _write(
            root / "scripts" / "addeffect" / "main.go",
            "package main\n\nfunc main() {}\n",
        )
        config = {
            "publish_mode": "ci",
            "targets": ["go"],
            "pipelines": {
                "go": {
                    "type": "go",
                    "local": False,
                    "target": "go",
                    "artifact": "library",
                }
            },
        }
        _write(root / ".rlsbl" / "config.json", json.dumps(config))
        return config

    def test_is_library_honours_declaration(self, tmp_path):
        config = self._library_project_with_dev_helper(tmp_path)
        target = GoTarget()
        assert target._is_library(str(tmp_path), config) is True

    def test_declared_binary_honoured_over_pure_module(self, tmp_path):
        """The declaration wins in the other direction too."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "lib.go", "package myapp\n")
        config = {"pipelines": {"go": {"type": "go", "local": False,
                                       "artifact": "binary"}}}
        target = GoTarget()
        assert target._is_library(str(tmp_path), config) is False

    def test_undeclared_falls_back_to_introspection(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        target = GoTarget()
        assert target._is_library(str(tmp_path)) is False

    def test_no_goreleaser_artifacts_for_declared_library(self, tmp_path):
        config = self._library_project_with_dev_helper(tmp_path)
        target = GoTarget()
        ctx = make_ctx(tmp_path, config)
        targets = [m["target"] for m in target.template_mappings(ctx)]
        assert ".goreleaser.yml" not in targets
        assert not any(t.endswith("version.go") for t in targets)

    def test_library_publish_setup_text(self, tmp_path):
        config = self._library_project_with_dev_helper(tmp_path)
        target = GoTarget()
        ctx = make_ctx(tmp_path, config)
        tvars = target.template_vars(str(tmp_path), ctx)
        assert "goreleaser" not in tvars["publishSetup"].lower()
        assert tvars["goreleaserMain"] == ""
