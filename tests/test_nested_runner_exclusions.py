"""A parent member's test runner skips the members nested inside it.

pytest collects from wherever it starts, so a Python parent (``sdk``) running
its own suite also collected a nested member's tests (``sdk/python/tests``)
and failed on that member's dependencies. Scaffold writes a path-exact
``--ignore=<path>`` for each nested member into the parent's pytest
``addopts`` (and each path into Deno's ``exclude``), and the
``nested-member-runner-exclusion`` check fails while one is missing. pytest
resolves ``--ignore`` against the directory it starts in, which is the
member's own directory in rlsbl's runs and in CI.
"""

import subprocess
import sys
import tomllib

from conftest import make_nested_workspace


def _check(root):
    from rlsbl import app
    from rlsbl.check_context import WorkspaceCheckContext
    from rlsbl.workspace import load_workspace

    ctx = WorkspaceCheckContext(
        project_root=root, workspace_root=root, config={},
        projects=load_workspace(str(root)),
    )
    return app._check_defs["nested-member-runner-exclusion"].impl(ctx)


def _scaffold(member_dir, monkeypatch):
    import rlsbl

    monkeypatch.chdir(member_dir)
    result = rlsbl.app.test(["scaffold", "--no-auto-commit"])
    assert result.exit_code == 0, result.stderr


def _addopts(pyproject):
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    return data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts") or []


def test_scaffold_writes_a_path_exact_ignore_for_each_nested_member(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    _scaffold(root / "sdk", monkeypatch)
    addopts = _addopts(root / "sdk" / "pyproject.toml")
    assert "--ignore=python" in addopts
    assert "--ignore=npm" in addopts


def test_the_parents_pytest_run_no_longer_collects_the_nested_members_tests(
    tmp_path, monkeypatch,
):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    _scaffold(root / "sdk", monkeypatch)
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:testisolation"],
        cwd=root / "sdk", capture_output=True, text=True,
    ).stdout
    assert "tests/test_it.py" in collected
    assert "python/tests/test_it.py" not in collected


def test_the_check_fails_while_an_exclusion_is_missing_and_scaffold_clears_it(
    tmp_path, monkeypatch,
):
    root = tmp_path / "ws"
    make_nested_workspace(root, "python")
    result = _check(root)
    assert result.status == "fail"
    text = " ".join(p.text for p in result.problems)
    assert "sdk/pyproject.toml" in text
    assert "--ignore=python" in text
    assert "rlsbl scaffold" in text
    # Apply the named fix.
    _scaffold(root / "sdk", monkeypatch)
    assert _check(root).status == "pass", [p.text for p in _check(root).problems]


def test_a_workspace_without_nesting_passes(tmp_path):
    from conftest import make_workspace

    root = tmp_path / "ws"
    (root / "a").mkdir(parents=True)
    (root / "a" / "pyproject.toml").write_text('[project]\nname = "a"\nversion = "0.1.0"\n')
    make_workspace(root, [{"path": "a", "name": "a"}])
    assert _check(root).status in ("pass", "skip")


def test_deno_gets_the_nested_member_in_its_exclude(tmp_path, monkeypatch):
    import json

    from rlsbl.nested_exclusions import (
        apply_nested_member_exclusions,
        missing_exclusions,
    )
    from rlsbl.scratch_dirs import DENO_CONFIG_EXCLUDE

    app = tmp_path / "app"
    (app / "sub").mkdir(parents=True)
    (app / "deno.json").write_text('{\n  "name": "app"\n}\n')
    assert missing_exclusions(DENO_CONFIG_EXCLUDE, str(app), ["sub"]) == (
        str(app / "deno.json"), ["sub"],
    )
    monkeypatch.chdir(app)
    created, _skipped, warnings = apply_nested_member_exclusions(
        {"deno": "."}, [str(app / "sub")],
    )
    assert warnings == []
    assert json.loads((app / "deno.json").read_text())["exclude"] == ["sub"]
    assert missing_exclusions(DENO_CONFIG_EXCLUDE, str(app), ["sub"]) is None
