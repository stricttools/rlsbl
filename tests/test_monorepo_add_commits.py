"""`rlsbl monorepo add` leaves nothing it wrote uncommitted, and resolves paths from the workspace root.

Scaffolding a member whose file already matches its template (a Go member's
existing ``VERSION``) seeds that file's merge base without rewriting the file;
the seeded base was left out of the scaffold commit and stayed untracked.
"""

import os
import subprocess

import pytest


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def ws(tmp_path, monkeypatch):
    import rlsbl

    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "add@example.invalid")
    _git(root, "config", "user.name", "add")
    _git(root, "remote", "add", "origin", "git@github.com:example/nested.git")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.chdir(root)
    assert rlsbl.app.test(["monorepo", "init", "--root-dev-node"]).exit_code == 0
    tools = root / "tools"
    tools.mkdir()
    (tools / "go.mod").write_text("module github.com/example/nested/tools\n\ngo 1.22\n")
    (tools / "tools.go").write_text("package tools\n")
    (tools / "VERSION").write_text("0.1.0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "tools")
    return root


def test_monorepo_add_leaves_no_seeded_base_untracked(ws):
    import rlsbl

    result = rlsbl.app.test(["monorepo", "add", "tools", "--releasable", "tools"])
    assert result.exit_code == 0, result.stderr
    assert _git(ws, "status", "--short", "--untracked-files=all") == ""


def test_monorepo_add_resolves_the_path_from_the_workspace_root(ws, monkeypatch):
    """Run from inside another directory, `tools` still means <root>/tools."""
    import rlsbl
    from rlsbl.workspace import load_workspace

    elsewhere = ws / "docs"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = rlsbl.app.test(["monorepo", "add", "tools", "--releasable", "tools"])
    assert result.exit_code == 0, result.stderr
    assert "tools" in [p["path"] for p in load_workspace(str(ws))]
    assert not os.path.exists(elsewhere / "tools")
