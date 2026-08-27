"""The shared module-prefix rule, and the similarly-named-module bug it fixes.

``rlsbl/module_paths.py`` holds the one answer to "is import X inside module
Y?".  Before it existed the answer was spelled inline in the Go import
scanner, both dead-code detectors, and the strictcli entry-point detector --
and the strictcli detector's second, inline copy had dropped the separator
check, so any module whose NAME merely began with ``github.com/smm-h/strictcli``
was read as strictcli itself.

The class of defect is what the shared helper removes, so the tests below pin
both the rule and every migrated call site's behavior on a
similarly-named-module input.
"""

import os

import pytest

from rlsbl.module_paths import (
    DOT_SEP,
    GO_SEP,
    dotted_under_module,
    go_import_under_module,
    rewrite_module_prefix,
    under_module_prefix,
)


class TestTheRule:
    def test_the_prefix_itself_is_inside(self):
        assert go_import_under_module("github.com/o/foo", "github.com/o/foo")

    def test_a_subpackage_is_inside(self):
        assert go_import_under_module("github.com/o/foo/bar", "github.com/o/foo")

    def test_a_similarly_named_module_is_not_inside(self):
        """The whole point: a shared letter run is not containment."""
        assert not go_import_under_module("github.com/o/foobar", "github.com/o/foo")
        assert not go_import_under_module(
            "github.com/o/foobar/baz", "github.com/o/foo"
        )

    def test_dotted_names_use_the_dot_boundary(self):
        assert dotted_under_module("a.b.c", "a.b")
        assert not dotted_under_module("a.bc", "a.b")

    def test_an_empty_prefix_matches_nothing(self):
        assert not under_module_prefix("anything", "", sep=GO_SEP)

    def test_the_separator_has_no_default(self):
        with pytest.raises(TypeError):
            under_module_prefix("a/b", "a")  # type: ignore[call-arg]

    def test_rewrite_reroots_only_what_is_inside(self):
        assert rewrite_module_prefix(
            "github.com/o/foo/bar", "github.com/o/foo", "github.com/n/qux",
            sep=GO_SEP,
        ) == "github.com/n/qux/bar"
        assert rewrite_module_prefix(
            "github.com/o/foobar", "github.com/o/foo", "github.com/n/qux",
            sep=GO_SEP,
        ) == "github.com/o/foobar"
        assert rewrite_module_prefix("a.b.c", "a.b", "x.y", sep=DOT_SEP) == "x.y.c"


class TestStrictcliDetectorDoesNotMatchASimilarlyNamedModule:
    """RED-GREEN: the live bug the consolidation fixes.

    ``_go_file_imports_strictcli`` used a bare ``startswith`` against
    ``github.com/smm-h/strictcli``, so a file importing an unrelated
    ``github.com/smm-h/strictcli-extras/...`` was reported as importing
    strictcli.  In a repo with several main packages that is the tie-breaker
    that picks the CLI entry point, so the wrong package was named as the
    project's CLI -- and the schema dump then ran the wrong binary.
    """

    def _go_file(self, tmp_path, import_path):
        path = tmp_path / "main.go"
        path.write_text(
            "package main\n\n"
            "import (\n"
            f'\t"{import_path}"\n'
            ")\n\n"
            "func main() {}\n"
        )
        return str(path)

    def test_a_similarly_named_module_is_not_strictcli(self, tmp_path):
        from rlsbl.strictcli_detect import _go_file_imports_strictcli

        path = self._go_file(tmp_path, "github.com/smm-h/strictcli-extras/cli")
        assert not _go_file_imports_strictcli(path)

    def test_the_real_module_still_matches(self, tmp_path):
        from rlsbl.strictcli_detect import _go_file_imports_strictcli

        assert _go_file_imports_strictcli(
            self._go_file(tmp_path, "github.com/smm-h/strictcli/go")
        )
        assert _go_file_imports_strictcli(
            self._go_file(tmp_path, "github.com/smm-h/strictcli")
        )

    def test_go_mod_require_already_used_the_boundary(self, tmp_path):
        """The other copy in the same module was already correct -- pinned so
        the consolidation cannot regress it."""
        from rlsbl.strictcli_detect import _go_mod_has_strictcli

        (tmp_path / "go.mod").write_text(
            "module example.com/app\n\n"
            "require (\n"
            "\tgithub.com/smm-h/strictcli-extras/go v0.1.0\n"
            ")\n"
        )
        assert not _go_mod_has_strictcli(str(tmp_path))

        (tmp_path / "go.mod").write_text(
            "module example.com/app\n\n"
            "require github.com/smm-h/strictcli/go v0.1.0\n"
        )
        assert _go_mod_has_strictcli(str(tmp_path))


class TestMigratedCallSitesHonourTheBoundary:
    """Each migrated site, asked the similarly-named-module question."""

    def test_go_import_scanner(self):
        from rlsbl.import_scanners import GoImportScanner

        module_to_name = {"github.com/o/foo": "foo"}
        assert GoImportScanner._match_workspace_import(
            "github.com/o/foo/pkg", module_to_name
        ) == "foo"
        assert GoImportScanner._match_workspace_import(
            "github.com/o/foobar/pkg", module_to_name
        ) is None

    def test_dead_go_package_detector(self, tmp_path):
        """An import of a similarly-named package must not keep a package alive."""
        from rlsbl.dep_validation import find_dead_go_packages

        (tmp_path / "go.mod").write_text("module example.com/app\n")
        pkg = tmp_path / "internal" / "used"
        pkg.mkdir(parents=True)
        (pkg / "used.go").write_text("package used\n")
        # A sibling importing "internal/usedother" -- a DIFFERENT package whose
        # name starts with the same letters -- must not save internal/used.
        other = tmp_path / "internal" / "usedother"
        other.mkdir(parents=True)
        (other / "o.go").write_text("package usedother\n")
        (tmp_path / "main.go").write_text(
            "package main\n\n"
            'import "example.com/app/internal/usedother"\n\n'
            "func main() {}\n"
        )

        dead = find_dead_go_packages(str(tmp_path))
        assert os.path.join("internal", "used") in dead
        assert os.path.join("internal", "usedother") not in dead

    def test_dead_python_module_detector_boundary(self):
        """The dotted rule, exercised through the detector's own helper."""
        assert dotted_under_module("pkg.mod.sub", "pkg.mod")
        assert not dotted_under_module("pkg.module", "pkg.mod")

    def test_jvm_import_scanner(self, tmp_path):
        from rlsbl.import_scanners import JavaImportScanner

        src = tmp_path / "Main.java"
        src.write_text("import com.example.foobar.Thing;\n")
        results = JavaImportScanner().scan(
            str(tmp_path), {"foo"}, package_map={"com.example.foo": "foo"},
        )
        assert results == []

        src.write_text("import com.example.foo.Thing;\n")
        results = JavaImportScanner().scan(
            str(tmp_path), {"foo"}, package_map={"com.example.foo": "foo"},
        )
        assert [r.package_name for r in results] == ["foo"]
