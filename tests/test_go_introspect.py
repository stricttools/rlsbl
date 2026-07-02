"""Tests for rlsbl.go_introspect -- toolchain-backed Go project classification.

The helper is the single source of truth for project-level Go detection
(library vs binary, entry-point location). It must handle layouts that
hand-rolled main.go globbing misdetects: entry files not named main.go
(cmd/x/cli.go), _test.go files in package main, and broken-root layouts
(root package main with no func main plus a real main under cmd/).
"""

import json

import pytest

from rlsbl.go_introspect import (
    GoIntrospectError,
    list_main_packages,
    list_packages,
    resolve_main_package_dir,
)


GO_MOD = "module github.com/user/proj\n\ngo 1.21\n"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestListMainPackages:
    def test_cli_go_entry_file_detected(self, tmp_path):
        """cmd/x/cli.go (no file named main.go) is detected as a main package."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        mains = list_main_packages(str(tmp_path))
        assert [p.rel_dir for p in mains] == ["./cmd/x"]

    def test_root_main_go(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        mains = list_main_packages(str(tmp_path))
        assert [p.rel_dir for p in mains] == ["."]

    def test_library_has_no_main_packages(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "lib.go", "package proj\n\nfunc Hello() {}\n")
        assert list_main_packages(str(tmp_path)) == []

    def test_broken_root_layout_reports_both_mains(self, tmp_path):
        """Root package main with no func main + real main in cmd/ yields
        two main packages -- the caller sees the ambiguity instead of a
        silent misdetection."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "version.go", "package main\n\nvar Version string\n")
        _write(tmp_path / "cmd" / "x" / "main.go", "package main\n\nfunc main() {}\n")
        mains = list_main_packages(str(tmp_path))
        assert sorted(p.rel_dir for p in mains) == [".", "./cmd/x"]

    def test_test_files_do_not_confuse_package_name(self, tmp_path):
        """_test.go files in the package main dir don't change the package
        name go list reports (a hand-rolled scan could trip on them)."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        _write(
            tmp_path / "cmd" / "x" / "cli_test.go",
            'package main\n\nimport "testing"\n\nfunc TestX(t *testing.T) {}\n',
        )
        mains = list_main_packages(str(tmp_path))
        assert [p.rel_dir for p in mains] == ["./cmd/x"]

    def test_multi_binary_layout(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "a" / "main.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "cmd" / "b" / "main.go", "package main\n\nfunc main() {}\n")
        mains = list_main_packages(str(tmp_path))
        assert sorted(p.rel_dir for p in mains) == ["./cmd/a", "./cmd/b"]

    def test_unresolvable_imports_tolerated(self, tmp_path):
        """Packages whose imports can't be resolved (no network, no go.sum)
        are still enumerated with their package name."""
        _write(tmp_path / "go.mod", GO_MOD + "\nrequire github.com/smm-h/strictcli/go v0.9.0\n")
        _write(
            tmp_path / "cmd" / "x" / "cli.go",
            'package main\n\nimport _ "github.com/smm-h/strictcli/go/strictcli"\n\nfunc main() {}\n',
        )
        mains = list_main_packages(str(tmp_path))
        assert [p.rel_dir for p in mains] == ["./cmd/x"]

    def test_no_go_files_returns_empty(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        assert list_packages(str(tmp_path)) == []

    def test_missing_go_mod_raises(self, tmp_path):
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        with pytest.raises(GoIntrospectError, match="go.mod"):
            list_packages(str(tmp_path))

    def test_missing_go_binary_raises(self, tmp_path, monkeypatch):
        """A missing go toolchain is a hard error, never an empty result."""
        _write(tmp_path / "go.mod", GO_MOD)
        monkeypatch.setattr("rlsbl.go_introspect.shutil.which", lambda name: None)
        with pytest.raises(GoIntrospectError, match="'go' not found on PATH"):
            list_packages(str(tmp_path))

    def test_go_list_failure_surfaces_stderr(self, tmp_path):
        """go list errors (e.g. malformed go.mod) surface stderr in the message."""
        _write(tmp_path / "go.mod", "this is not a go.mod\n")
        with pytest.raises(GoIntrospectError, match="go list"):
            list_packages(str(tmp_path))


class TestResolveMainPackageDir:
    def _config(self, install_paths=None):
        entry = {"type": "go", "local": True}
        if install_paths is not None:
            entry["install_paths"] = install_paths
        return {"pipelines": {"go": entry}}

    def test_single_main_no_declaration(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        assert resolve_main_package_dir(str(tmp_path), self._config()) == "./cmd/x"

    def test_root_main_no_declaration(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "main.go", "package main\n\nfunc main() {}\n")
        assert resolve_main_package_dir(str(tmp_path), self._config()) == "."

    def test_multiple_mains_no_declaration_is_hard_error(self, tmp_path):
        """Ambiguous layouts must error with the detected mains listed,
        never silently fall back to '.'."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "version.go", "package main\n\nvar Version string\n")
        _write(tmp_path / "cmd" / "x" / "main.go", "package main\n\nfunc main() {}\n")
        with pytest.raises(GoIntrospectError) as exc:
            resolve_main_package_dir(str(tmp_path), self._config())
        assert "install_paths" in str(exc.value)
        assert "./cmd/x" in str(exc.value)

    def test_declaration_resolves_ambiguity(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "version.go", "package main\n\nvar Version string\n")
        _write(tmp_path / "cmd" / "x" / "main.go", "package main\n\nfunc main() {}\n")
        config = self._config(install_paths=["./cmd/x"])
        assert resolve_main_package_dir(str(tmp_path), config) == "./cmd/x"

    def test_declared_path_must_be_main_package(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "x" / "cli.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "internal" / "lib.go", "package lib\n")
        config = self._config(install_paths=["./internal"])
        with pytest.raises(GoIntrospectError, match="not a main package"):
            resolve_main_package_dir(str(tmp_path), config)

    def test_no_mains_is_hard_error(self, tmp_path):
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "lib.go", "package proj\n")
        with pytest.raises(GoIntrospectError, match="no main packages"):
            resolve_main_package_dir(str(tmp_path), self._config())

    def test_multiple_declared_paths_is_hard_error_for_single_binary(self, tmp_path):
        """version.go placement and goreleaser main need exactly one dir."""
        _write(tmp_path / "go.mod", GO_MOD)
        _write(tmp_path / "cmd" / "a" / "main.go", "package main\n\nfunc main() {}\n")
        _write(tmp_path / "cmd" / "b" / "main.go", "package main\n\nfunc main() {}\n")
        config = self._config(install_paths=["./cmd/a", "./cmd/b"])
        with pytest.raises(GoIntrospectError, match="exactly one"):
            resolve_main_package_dir(str(tmp_path), config)
