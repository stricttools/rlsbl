"""Each ecosystem's own test runner is told to skip the scratch directories.

``rlsbl scaffold`` creates ``experiments/`` and ``screenshots/`` at every
project's root.  A half-finished probe left in one of them must not be able to
break a suite it has nothing to do with, so scaffold also writes the one
setting the project's own test runner honours: ``norecursedirs`` for pytest, a
nested module file for ``go test ./...``, and ``exclude`` in the Deno
configuration file.  Which mechanism a target uses is declared on the target
itself, so the answer is in the support matrix rather than in a hand-kept list.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from rlsbl.scratch_dirs import (
    DENO_CONFIG_EXCLUDE,
    GO_NESTED_MODULE,
    NO_TEST_RUNNER_RECURSION,
    PYTEST_DEFAULT_NORECURSEDIRS,
    PYTEST_NORECURSEDIRS,
    RUNNER_CHOSEN_BY_PROJECT,
    SCRATCH_DIR_NAMES,
    SCRATCH_GO_MODULE_FILENAME,
    SCRATCH_TEST_EXCLUSION_MECHANISMS,
    apply_scratch_test_exclusions,
    scratch_mechanisms,
    scratch_template_mappings,
    scratch_template_vars,
)
from rlsbl.targets import TARGETS

REPO_ROOT = Path(__file__).resolve().parent.parent

_PYPROJECT = '[project]\nname = "mylib"\nversion = "0.1.0"\n'

_GO_MOD = "module example.com/mylib\n\ngo 1.21\n"

_DENO_JSON = '{\n  "name": "@scope/mylib",\n  "version": "0.1.0"\n}\n'


def _child_pytest(cwd):
    """Run a real pytest against *cwd*, with this repo's own plugins off.

    The rlsbl suite runs under the stricttest plugin, which aborts any pytest
    process whose configuration does not declare its stances.  The fixture
    project is a plain project that declares none, so the child run disables
    the plugins the parent environment happens to carry.
    """
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "-p", "no:stricttest",
            "-p", "no:cacheprovider",
            "-p", "no:xdist",
            "-p", "no:randomly",
        ],
        cwd=str(cwd), capture_output=True, text=True,
    )


def _plant_python_probes(root):
    """A passing real test, plus a failing one in each scratch directory."""
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_real.py").write_text("def test_real():\n    assert True\n")
    for name in SCRATCH_DIR_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "test_probe.py").write_text(
            "def test_half_finished_probe():\n    assert False\n"
        )


def _read_norecursedirs(pyproject_path):
    doc = tomlkit.parse(Path(pyproject_path).read_text())
    return list(doc["tool"]["pytest"]["ini_options"]["norecursedirs"])


class TestMechanismIsDeclaredPerTarget:
    """Which mechanism an ecosystem uses is a declared per-target fact."""

    def test_every_target_declares_a_known_mechanism(self):
        for name, target in TARGETS.items():
            assert target.scratch_test_exclusion in SCRATCH_TEST_EXCLUSION_MECHANISMS, (
                f"target '{name}' declares an unknown scratch-exclusion mechanism"
            )

    def test_python_declares_pytest_norecursedirs(self):
        assert TARGETS["pypi"].scratch_test_exclusion == PYTEST_NORECURSEDIRS

    def test_go_declares_the_nested_module(self):
        assert TARGETS["go"].scratch_test_exclusion == GO_NESTED_MODULE

    def test_deno_declares_the_config_exclude(self):
        assert TARGETS["deno"].scratch_test_exclusion == DENO_CONFIG_EXCLUDE

    def test_npm_declares_that_the_project_chooses_the_runner(self):
        """npm's `test` script names a runner rlsbl neither picks nor configures."""
        assert TARGETS["npm"].scratch_test_exclusion == RUNNER_CHOSEN_BY_PROJECT

    def test_an_ecosystem_whose_runner_never_recurses_says_so(self):
        """Maven compiles only its declared test source set, so nothing is needed."""
        assert TARGETS["maven"].scratch_test_exclusion == NO_TEST_RUNNER_RECURSION

    def test_mechanisms_are_resolved_from_target_names(self):
        assert scratch_mechanisms({"pypi"}) == frozenset({PYTEST_NORECURSEDIRS})
        assert scratch_mechanisms({"go", "pypi"}) == frozenset(
            {GO_NESTED_MODULE, PYTEST_NORECURSEDIRS}
        )


class TestPytestNorecursedirs:
    """A Python project gets the pytest setting, and pytest honours it."""

    def test_scaffold_writes_the_setting_into_pyproject(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)

        created, _skipped, warnings = apply_scratch_test_exclusions({"pypi": "."})

        assert warnings == []
        assert "pyproject.toml" in {t for t, _ in created}
        value = _read_norecursedirs(tmp_path / "pyproject.toml")
        for name in SCRATCH_DIR_NAMES:
            assert name in value

    def test_pytest_own_defaults_are_kept(self, tmp_path, monkeypatch):
        """norecursedirs REPLACES pytest's default, so the default is re-stated."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)

        apply_scratch_test_exclusions({"pypi": "."})

        value = _read_norecursedirs(tmp_path / "pyproject.toml")
        for pattern in PYTEST_DEFAULT_NORECURSEDIRS:
            assert pattern in value

    def test_declared_default_matches_the_installed_pytest(self):
        """The re-stated default is pytest's real one, not a stale copy of it.

        pytest exposes no public reader for an ini option's default, so this
        asks the installed pytest to register its own options against a parser
        and reads the answer back out. A pytest release that changes the
        default turns this red instead of silently narrowing every scaffolded
        project's collection.
        """
        import warnings as _warnings

        import _pytest.main

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            from _pytest.config.argparsing import Parser

            parser = Parser()
        _pytest.main.pytest_addoption(parser)
        _help, _type, default = parser._inidict["norecursedirs"]
        assert list(PYTEST_DEFAULT_NORECURSEDIRS) == list(default)

    def test_a_real_pytest_run_collects_neither_probe(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        _plant_python_probes(tmp_path)

        before = _child_pytest(tmp_path)
        assert before.returncode != 0, before.stdout

        apply_scratch_test_exclusions({"pypi": "."})

        after = _child_pytest(tmp_path)
        assert after.returncode == 0, after.stdout + after.stderr
        assert "1 passed" in after.stdout

    def test_rescaffolding_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)

        apply_scratch_test_exclusions({"pypi": "."})
        first = (tmp_path / "pyproject.toml").read_text()

        created, skipped, warnings = apply_scratch_test_exclusions({"pypi": "."})

        assert (tmp_path / "pyproject.toml").read_text() == first
        assert created == []
        assert warnings == []
        assert "pyproject.toml" in {t for t, _ in skipped}

    def test_unrelated_user_content_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        original = (
            "# a comment the user wrote\n"
            '[project]\nname = "mylib"\nversion = "0.1.0"\n\n'
            "[tool.pytest.ini_options]\n"
            "# why this project keeps its own patterns\n"
            'testpaths = ["tests"]\n'
            'norecursedirs = ["vendor"]\n\n'
            "[tool.ruff]\nline-length = 100\n"
        )
        (tmp_path / "pyproject.toml").write_text(original)

        apply_scratch_test_exclusions({"pypi": "."})

        text = (tmp_path / "pyproject.toml").read_text()
        assert "# a comment the user wrote" in text
        assert "# why this project keeps its own patterns" in text
        assert "line-length = 100" in text
        doc = tomlkit.parse(text)
        assert list(doc["tool"]["pytest"]["ini_options"]["testpaths"]) == ["tests"]
        value = list(doc["tool"]["pytest"]["ini_options"]["norecursedirs"])
        assert value[0] == "vendor"
        for name in SCRATCH_DIR_NAMES:
            assert name in value

    def test_a_pytest_ini_is_reported_rather_than_bypassed(self, tmp_path, monkeypatch):
        """pytest.ini outranks pyproject.toml, so writing there would be inert."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")

        _created, _skipped, warnings = apply_scratch_test_exclusions({"pypi": "."})

        assert any("pytest.ini" in w and "norecursedirs" in w for w in warnings)
        assert "[tool.pytest.ini_options]" not in (
            tmp_path / "pyproject.toml"
        ).read_text()

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(_PYPROJECT)

        created, _skipped, _warnings = apply_scratch_test_exclusions(
            {"pypi": "."}, dry_run=True,
        )

        assert "pyproject.toml" in {t for t, _ in created}
        assert (tmp_path / "pyproject.toml").read_text() == _PYPROJECT

    def test_rlsbl_own_pyproject_carries_the_setting(self):
        value = _read_norecursedirs(REPO_ROOT / "pyproject.toml")
        for name in SCRATCH_DIR_NAMES:
            assert name in value


class TestGoNestedModule:
    """A Go scratch directory carries its own module file, so the go tool skips it."""

    def test_scaffold_maps_a_module_file_into_each_scratch_dir(self):
        mappings = scratch_template_mappings(frozenset({GO_NESTED_MODULE}))
        targets = {m["target"] for m in mappings}
        for name in SCRATCH_DIR_NAMES:
            assert f"{name}/{SCRATCH_GO_MODULE_FILENAME}" in targets

    def test_a_non_go_project_gets_no_module_file(self):
        mappings = scratch_template_mappings(frozenset({PYTEST_NORECURSEDIRS}))
        targets = {m["target"] for m in mappings}
        assert not any(t.endswith("/go.mod") for t in targets)

    def test_the_scratch_gitignore_unignores_it_only_for_go(self, tmp_path, monkeypatch):
        from rlsbl.commands.init_cmd import process_mappings
        from rlsbl.targets.base import BaseTarget

        monkeypatch.chdir(tmp_path)
        tpl_dir = BaseTarget().shared_template_dir()
        mechanisms = frozenset({GO_NESTED_MODULE})
        mappings = scratch_template_mappings(mechanisms)
        process_mappings(tpl_dir, mappings, scratch_template_vars(mechanisms))

        for name in SCRATCH_DIR_NAMES:
            ignore = (tmp_path / name / ".gitignore").read_text()
            assert ignore.splitlines() == ["*", "!.gitignore", "!go.mod"]

    def test_the_scratch_gitignore_is_unchanged_without_go(self, tmp_path, monkeypatch):
        from rlsbl.commands.init_cmd import process_mappings
        from rlsbl.targets.base import BaseTarget

        monkeypatch.chdir(tmp_path)
        tpl_dir = BaseTarget().shared_template_dir()
        mechanisms = frozenset({PYTEST_NORECURSEDIRS})
        mappings = scratch_template_mappings(mechanisms)
        process_mappings(tpl_dir, mappings, scratch_template_vars(mechanisms))

        for name in SCRATCH_DIR_NAMES:
            ignore = (tmp_path / name / ".gitignore").read_text()
            assert ignore.splitlines() == ["*", "!.gitignore"]

    def test_the_module_file_is_committable(self, tmp_path, monkeypatch):
        """The ignore exception is what carries the marker into a fresh clone."""
        from rlsbl.commands.init_cmd import process_mappings
        from rlsbl.targets.base import BaseTarget

        from conftest import run_git

        monkeypatch.chdir(tmp_path)
        run_git(tmp_path, "init", "-q", "-b", "main")
        tpl_dir = BaseTarget().shared_template_dir()
        mechanisms = frozenset({GO_NESTED_MODULE})
        mappings = scratch_template_mappings(mechanisms)
        process_mappings(tpl_dir, mappings, scratch_template_vars(mechanisms))

        for name in SCRATCH_DIR_NAMES:
            probe = subprocess.run(
                ["git", "check-ignore", "-q", f"{name}/go.mod"],
                cwd=str(tmp_path), capture_output=True,
            )
            assert probe.returncode == 1, f"{name}/go.mod is ignored"

    @pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not available")
    def test_a_real_go_test_run_skips_a_planted_source_file(
        self, tmp_path, monkeypatch,
    ):
        from rlsbl.commands.init_cmd import process_mappings
        from rlsbl.targets.base import BaseTarget

        monkeypatch.chdir(tmp_path)
        (tmp_path / "go.mod").write_text(_GO_MOD)
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "a.go").write_text("package pkg\n\nfunc A() int { return 1 }\n")
        (pkg / "a_test.go").write_text(
            "package pkg\n\nimport \"testing\"\n\n"
            "func TestA(t *testing.T) {\n\tif A() != 1 {\n\t\tt.Fatal(\"no\")\n\t}\n}\n"
        )
        (tmp_path / "experiments").mkdir()
        (tmp_path / "experiments" / "probe.go").write_text(
            "package experiments\n\nthis half-finished probe does not compile\n"
        )

        before = subprocess.run(
            ["go", "test", "./..."], cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert before.returncode != 0, before.stdout

        tpl_dir = BaseTarget().shared_template_dir()
        mechanisms = frozenset({GO_NESTED_MODULE})
        mappings = scratch_template_mappings(mechanisms)
        process_mappings(tpl_dir, mappings, scratch_template_vars(mechanisms))

        after = subprocess.run(
            ["go", "test", "./..."], cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert after.returncode == 0, after.stdout + after.stderr


class TestDenoConfigExclude:
    """A Deno project's configuration file excludes both directories."""

    def test_scaffold_writes_the_exclude_entries(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "deno.json").write_text(_DENO_JSON)

        created, _skipped, warnings = apply_scratch_test_exclusions({"deno": "."})

        assert warnings == []
        assert "deno.json" in {t for t, _ in created}
        data = json.loads((tmp_path / "deno.json").read_text())
        for name in SCRATCH_DIR_NAMES:
            assert name in data["exclude"]

    def test_rescaffolding_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "deno.json").write_text(_DENO_JSON)

        apply_scratch_test_exclusions({"deno": "."})
        first = (tmp_path / "deno.json").read_text()
        created, skipped, _warnings = apply_scratch_test_exclusions({"deno": "."})

        assert (tmp_path / "deno.json").read_text() == first
        assert created == []
        assert "deno.json" in {t for t, _ in skipped}

    def test_existing_user_excludes_are_preserved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "deno.json").write_text(
            '{\n  "name": "@scope/mylib",\n  "version": "0.1.0",\n'
            '  "exclude": ["vendor"],\n  "tasks": {"dev": "deno run main.ts"}\n}\n'
        )

        apply_scratch_test_exclusions({"deno": "."})

        data = json.loads((tmp_path / "deno.json").read_text())
        assert data["exclude"][0] == "vendor"
        assert data["tasks"] == {"dev": "deno run main.ts"}
        for name in SCRATCH_DIR_NAMES:
            assert name in data["exclude"]

    def test_a_jsonc_config_is_reported_rather_than_rewritten(
        self, tmp_path, monkeypatch,
    ):
        """Rewriting JSON with comments would lose them, so the operator is told."""
        monkeypatch.chdir(tmp_path)
        original = '{\n  // the package name\n  "name": "@scope/mylib"\n}\n'
        (tmp_path / "deno.jsonc").write_text(original)

        _created, _skipped, warnings = apply_scratch_test_exclusions({"deno": "."})

        assert any("deno.jsonc" in w and "exclude" in w for w in warnings)
        assert (tmp_path / "deno.jsonc").read_text() == original

    @pytest.mark.skipif(
        shutil.which("deno") is None, reason="deno toolchain not available"
    )
    def test_a_real_deno_test_run_skips_a_planted_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "deno.json").write_text(_DENO_JSON)
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.ts").write_text("export function a(): number { return 1; }\n")
        (src / "mod_test.ts").write_text(
            'import { a } from "./mod.ts";\n'
            'Deno.test("a", () => { if (a() !== 1) throw new Error("no"); });\n'
        )
        (tmp_path / "experiments").mkdir()
        (tmp_path / "experiments" / "probe_test.ts").write_text(
            "this half-finished probe ( does not parse !!!\n"
        )

        before = subprocess.run(
            ["deno", "test"], cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert before.returncode != 0, before.stdout

        apply_scratch_test_exclusions({"deno": "."})

        after = subprocess.run(
            ["deno", "test"], cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert after.returncode == 0, after.stdout + after.stderr


class TestScaffoldWiresItUp:
    """The scaffold command applies the exclusions for the targets it scaffolds."""

    def test_shared_mappings_carry_the_go_module_for_a_go_project(self, tmp_path):
        from conftest import make_ctx

        ctx = make_ctx(tmp_path, config={"targets": ["go"]})
        mappings = TARGETS["go"].shared_template_mappings(ctx)
        targets = {m["target"] for m in mappings}
        for name in SCRATCH_DIR_NAMES:
            assert f"{name}/{SCRATCH_GO_MODULE_FILENAME}" in targets

    def test_shared_mappings_omit_it_for_a_python_project(self, tmp_path):
        from conftest import make_ctx

        ctx = make_ctx(tmp_path, config={"targets": ["pypi"]})
        mappings = TARGETS["pypi"].shared_template_mappings(ctx)
        targets = {m["target"] for m in mappings}
        assert not any(t.endswith("/go.mod") for t in targets)

    def test_a_missing_manifest_is_reported_not_crashed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        created, _skipped, warnings = apply_scratch_test_exclusions({"pypi": "."})

        assert created == []
        assert any("pyproject.toml" in w for w in warnings)
