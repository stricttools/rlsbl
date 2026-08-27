"""Per-axis conformance: the library-lint dispatch family.

Two taxonomies meet here and must stay distinct:

- the **language** taxonomy (``python``, ``go``, ``npm``, ``maven``) that the
  linter dispatches on, now declared once in ``rlsbl.lint.languages.LANGUAGES``;
- the **target** taxonomy (``pypi``, ``go``, ``npm``, ``maven``, ...) that the
  release flow dispatches on, whose participation in library lint is now a
  single property, ``ReleaseTarget.lint_language``.

The dispatch used to be four parallel language lists that could drift, two of
which answered an unknown language with a bare ``return None`` the caller
silently ignored. Both halves are pinned here.
"""

import pytest

from rlsbl.lint import (
    _create_import_scanner,
    _create_linter,
    _detect_languages,
    lint_library,
    scan_imports,
)
from rlsbl.lint.config import load_language_config
from rlsbl.lint.languages import LANGUAGES, LANGUAGES_BY_NAME, get_language
from rlsbl.targets import TARGETS, targets_with_library_lint


class TestLanguageTableIsTheAuthority:
    """Everything the linter knows about a language comes from one table."""

    @pytest.mark.parametrize("lang", [lang.name for lang in LANGUAGES])
    def test_detection_uses_the_declared_manifests(self, tmp_path, lang):
        language = LANGUAGES_BY_NAME[lang]
        for manifest in language.manifests:
            d = tmp_path / f"{lang}-{manifest}"
            d.mkdir()
            (d / manifest).write_text("")
            assert lang in _detect_languages(str(d))

    def test_an_empty_directory_detects_no_language(self, tmp_path):
        assert _detect_languages(str(tmp_path)) == []

    @pytest.mark.parametrize("lang", [lang.name for lang in LANGUAGES])
    def test_every_language_has_a_linter_for_both_parser_types(self, lang):
        for parser_type in ("ast", "regex"):
            linter = _create_linter(lang, parser_type)
            assert linter is not None
            assert hasattr(linter, "lint")

    def test_default_excludes_come_from_the_table(self):
        """Python's exclusions are the table's, not a second hard-coded list."""
        assert "conftest.py" in LANGUAGES_BY_NAME["python"].default_excludes

    def test_default_forbidden_imports_come_from_the_table(self):
        """The config loader reads the same table, not a parallel dict."""
        assert (
            tuple(load_language_config(".", "python").forbidden_imports)
            == LANGUAGES_BY_NAME["python"].default_forbidden_imports
        )
        # maven's linter shells out, so it declares no forbidden imports.
        assert load_language_config(".", "maven").forbidden_imports == []

    def test_an_absent_import_scanner_must_state_a_reason(self):
        for language in LANGUAGES:
            if language.import_scanner is None:
                assert language.scanner_absent_reason, language.name


class TestNoSilentFallthrough:
    """An unknown language is a hard error, never a quiet no-op."""

    def test_create_linter_raises_on_an_unknown_language(self):
        with pytest.raises(ValueError, match="unknown lint language 'rust'"):
            _create_linter("rust", "ast")

    def test_create_import_scanner_raises_on_an_unknown_language(self):
        with pytest.raises(ValueError, match="unknown lint language 'rust'"):
            _create_import_scanner("rust")

    def test_get_language_names_the_declared_set_in_its_error(self):
        with pytest.raises(ValueError) as exc:
            get_language("cobol")
        for name in LANGUAGES_BY_NAME:
            assert name in str(exc.value)

    def test_scan_imports_contributes_nothing_for_a_language_with_no_scanner(
        self, tmp_path,
    ):
        """Go declares no project-wide scanner; dep_validation scans it per file.

        Which ecosystems the import-analysis checks cover is not decided here
        at all -- it is the targets' answer (``supports_import_analysis``), and
        an ecosystem out of scope is named in the check's own skip line.
        """
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.22\n")
        assert LANGUAGES_BY_NAME["go"].import_scanner is None
        assert scan_imports(str(tmp_path)) == set()

    def test_scan_imports_collects_from_a_scannable_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1.0"\n'
        )
        (tmp_path / "lib.py").write_text("import os\n")
        result = scan_imports(str(tmp_path))
        assert {r.top_level for r in result} == {"os"}

    def test_scan_imports_of_an_empty_directory_is_still_a_set(self, tmp_path):
        assert scan_imports(str(tmp_path)) == set()


class TestTargetSideBridge:
    """`lint_language` is the single link between the two taxonomies."""

    def test_lint_scope_is_derived_from_the_registry(self):
        derived = targets_with_library_lint()
        assert derived == frozenset(
            n for n, t in TARGETS.items() if t.lint_language is not None
        )
        # The four in scope before the migration are still the four in scope.
        assert derived == {"pypi", "go", "npm", "maven"}

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_every_declared_lint_language_exists_in_the_table(self, name):
        language = TARGETS[name].lint_language
        if language is not None:
            assert language in LANGUAGES_BY_NAME, (
                f"target '{name}' declares lint_language '{language}', which "
                f"the LANGUAGES table does not know about"
            )

    def test_pypi_maps_to_python_not_to_itself(self):
        """The two taxonomies are distinct: the target is not the language."""
        assert TARGETS["pypi"].lint_language == "python"
        assert "pypi" not in LANGUAGES_BY_NAME

    def test_a_target_out_of_scope_declares_no_language(self):
        assert TARGETS["zig"].lint_language is None
        assert TARGETS["plain"].lint_language is None


class TestLintLibraryStillRuns:
    """The supported side behaves exactly as before the migration."""

    def test_python_library_lint_flags_a_forbidden_import(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1.0"\n'
        )
        (tmp_path / "lib.py").write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert any("argparse" in r.message for r in results)

    def test_default_exclusions_keep_tests_out_of_lint(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1.0"\n'
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("import argparse\n")
        results = lint_library(str(tmp_path))
        assert not any("argparse" in r.message for r in results)

    def test_a_project_with_no_language_lints_nothing(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        assert lint_library(str(tmp_path)) == []
