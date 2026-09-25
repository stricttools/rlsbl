"""The ``package_rename`` target axis and the per-registry package-name rules.

Every registered target declares how its package name is renamed: in its own
manifest by rlsbl (``manifest-field``), through the Go module-path rewrite
(``go-module-path``), or not by rlsbl at all (``unsupported``). Each also names
the manifest and field that carries the name, which is what an operator edits by
hand when rlsbl cannot.
"""

import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.base import (
    PACKAGE_RENAME_GO_MODULE_PATH,
    PACKAGE_RENAME_MANIFEST_FIELD,
    PACKAGE_RENAME_POLICIES,
    PACKAGE_RENAME_UNSUPPORTED,
)
from rlsbl.targets.introspect import target_axis_answers


class TestAxis:
    def test_every_target_answers_from_the_closed_vocabulary(self):
        answers = target_axis_answers()
        for name, row in answers.items():
            assert row["package_rename"] in PACKAGE_RENAME_POLICIES, name

    def test_the_ruled_answers(self):
        assert TARGETS["npm"].package_rename == PACKAGE_RENAME_MANIFEST_FIELD
        assert TARGETS["pypi"].package_rename == PACKAGE_RENAME_MANIFEST_FIELD
        assert TARGETS["go"].package_rename == PACKAGE_RENAME_GO_MODULE_PATH
        for name, target in TARGETS.items():
            if name not in ("npm", "pypi", "go"):
                assert target.package_rename == PACKAGE_RENAME_UNSUPPORTED, name

    def test_every_class_declares_it_itself(self):
        """Declared, never inherited: a new target cannot answer by accident."""
        for name, target in TARGETS.items():
            assert "package_rename" in type(target).__dict__, name
            assert "package_name_field" in type(target).__dict__, name

    def test_every_name_reading_target_names_its_field(self):
        for name, target in TARGETS.items():
            if target.supports_read_name:
                assert target.package_name_field.strip(), name


class TestManifestRename:
    def test_npm_renames_only_the_name(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{\n  "name": "widget",\n  "version": "1.0.0",\n'
            '  "bin": {\n    "widget": "bin/widget"\n  }\n}\n'
        )
        plan = TARGETS["npm"].package_rename_plan(str(tmp_path), "widget", "gadget")
        assert plan.occurrences == 1
        assert '"name": "gadget"' in plan.new_text
        assert '"widget": "bin/widget"' in plan.new_text
        assert plan.new_text.endswith("}\n")

    def test_pypi_renames_only_the_name(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "widget"  # the name\nversion = "1.0.0"\n\n'
            '[project.scripts]\nwidget = "widget:main"\n'
        )
        plan = TARGETS["pypi"].package_rename_plan(str(tmp_path), "widget", "gadget")
        assert plan.occurrences == 1
        assert 'name = "gadget"  # the name' in plan.new_text
        assert 'widget = "widget:main"' in plan.new_text

    def test_a_manifest_naming_something_else_counts_zero(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "other"}\n')
        plan = TARGETS["npm"].package_rename_plan(str(tmp_path), "widget", "gadget")
        assert plan.occurrences == 0


class TestNpmNameRules:
    @pytest.mark.parametrize("name", ["gadget", "gadget-x", "gadget.x", "g_2", "a" * 214])
    def test_valid(self, name):
        assert TARGETS["npm"].package_name_problems(name) == []

    @pytest.mark.parametrize("name, needle", [
        ("", "empty"),
        ("a" * 215, "214"),
        ("Gadget", "uppercase"),
        (".gadget", "period"),
        ("_gadget", "underscore"),
        (" gadget", "space"),
        ("gad get", "URL"),
        ("gadget!", "special"),
        ("node_modules", "blocked"),
        ("http", "core module"),
    ])
    def test_invalid(self, name, needle):
        problems = TARGETS["npm"].package_name_problems(name)
        assert any(needle in p for p in problems), problems


class TestPypiNameRules:
    @pytest.mark.parametrize("name", ["gadget", "Gadget", "gad.get", "gad_get-2", "g"])
    def test_valid(self, name):
        assert TARGETS["pypi"].package_name_problems(name) == []

    @pytest.mark.parametrize("name", ["", "-gadget", "gadget-", "gad get", "gad!get"])
    def test_invalid(self, name):
        problems = TARGETS["pypi"].package_name_problems(name)
        assert problems and "PEP 508" in problems[0], problems


class TestGoNameRules:
    def test_available(self):
        assert TARGETS["go"].package_name_problems("gadget") == []

    @pytest.mark.parametrize("name, needle, clean", [
        ("gad_get", "underscore", "gadget"),
        ("Gadget", "uppercase", "gadget"),
    ])
    def test_discouraged_names_the_clean_spelling(self, name, needle, clean):
        problems = TARGETS["go"].package_name_problems(name)
        assert any(needle in p for p in problems), problems
        assert any(f"--to {clean}" in p for p in problems), problems

    @pytest.mark.parametrize("name", ["gad-get", "func", "http"])
    def test_invalid_and_taken_refuse(self, name):
        assert TARGETS["go"].package_name_problems(name)
