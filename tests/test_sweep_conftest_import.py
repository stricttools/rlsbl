"""Tests for the conftest-import rewriter the fixture sweep scripts share.

A sweep script wraps some expression in a ``conftest`` helper and then adds
that helper to the module's conftest import. Rebuilding that import from the
imported names alone drops every ``as`` clause, leaving the module calling a
name it no longer imports -- damage the sweep was never asked to do, in files
it only visited. That happened once and had to be repaired by hand.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

#: Every sweep script that rewrites a conftest import, with the helper it adds.
SWEEP_SCRIPTS = [
    ("sweep_workspace_fixtures", "workspace_toml"),
    ("sweep_declared_members", "declared_members"),
    ("sweep_save_workspace_root", "with_root_member"),
]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def shared():
    return _load("sweep_conftest_import")


class TestSharedRewriter:
    def test_alias_is_preserved(self, shared):
        source = "from conftest import git_head, run_git as _run_git\n\nx = 1\n"
        updated = shared.ensure_conftest_import(source, "workspace_toml")
        assert "run_git as _run_git" in updated
        assert "workspace_toml" in updated

    def test_an_aliased_wrapper_is_not_added_twice(self, shared):
        source = "from conftest import workspace_toml as _ws\n"
        assert shared.ensure_conftest_import(source, "workspace_toml") == source

    def test_already_imported_is_untouched(self, shared):
        source = "from conftest import make_workspace, workspace_toml\n"
        assert shared.ensure_conftest_import(source, "workspace_toml") == source

    def test_names_stay_sorted_by_the_imported_name(self, shared):
        source = "from conftest import zeta as a_zeta, make_commit\n"
        updated = shared.ensure_conftest_import(source, "run_git")
        assert updated.splitlines()[0] == (
            "from conftest import make_commit, run_git, zeta as a_zeta"
        )

    def test_no_conftest_import_gets_a_fresh_one(self, shared):
        source = "import os\n\nx = 1\n"
        updated = shared.ensure_conftest_import(source, "workspace_toml")
        assert "from conftest import workspace_toml" in updated
        assert "import os" in updated

    def test_a_module_with_no_imports_at_all(self, shared):
        updated = shared.ensure_conftest_import("x = 1\n", "workspace_toml")
        assert updated.startswith("from conftest import workspace_toml\n")


@pytest.mark.parametrize("module_name,wrapper", SWEEP_SCRIPTS)
class TestEverySweepScriptPreservesAliases:
    """The bug was in a copy of this helper in each script, not in one."""

    def test_alias_survives(self, module_name, wrapper):
        module = _load(module_name)
        source = "from conftest import git_head, run_git as _run_git\n\nx = 1\n"
        updated = module._ensure_import(source)
        assert "run_git as _run_git" in updated
        assert wrapper in updated

    def test_the_shared_rewriter_is_the_one_used(self, module_name, wrapper, shared):
        module = _load(module_name)
        source = "from conftest import run_git as _run_git\n"
        assert module._ensure_import(source) == shared.ensure_conftest_import(
            source, wrapper
        )
