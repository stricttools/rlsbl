"""Scaffolding a member leaves the merge bases of the members nested inside it.

A releasable member keeps its merge bases under its releasable's state
directory at its own path, so a parent's bases directory
(``bases/gfx``) contains a same-releasable nested member's
(``bases/gfx/shader``). The orphan sweep walked the whole of it and deleted
every one of the nested member's bases as orphans of the parent.
"""

import subprocess

from conftest import make_nested_workspace


def test_scaffolding_a_parent_keeps_a_nested_members_bases(tmp_path, monkeypatch):
    import rlsbl

    root = tmp_path / "ws"
    make_nested_workspace(root, "shared")
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/nested.git"],
        cwd=root, check=True,
    )
    bases = root / ".rlsbl-monorepo" / "releasables" / "gfx" / "bases" / "gfx" / "shader"

    monkeypatch.chdir(root / "gfx" / "shader")
    assert rlsbl.app.test(["scaffold", "--no-auto-commit"]).exit_code == 0
    before = sorted(p.relative_to(bases) for p in bases.rglob("*") if p.is_file())
    assert before, "the nested member's scaffold wrote no merge bases"

    monkeypatch.chdir(root / "gfx")
    result = rlsbl.app.test(["scaffold", "--no-auto-commit"])
    assert result.exit_code == 0, result.stderr
    assert "bases/gfx/shader" not in result.stdout
    after = sorted(p.relative_to(bases) for p in bases.rglob("*") if p.is_file())
    assert after == before
