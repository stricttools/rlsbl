"""Scaffolding a releasable member writes release state only where the
workspace keeps it.

A releasable member's own ``.rlsbl/`` may hold only what
:func:`rlsbl.releasable_cleanup.verify_minimal_rlsbl` allows, and the
``releasable-residue`` check refuses anything else. Scaffold used to write the
standalone layout into the member anyway -- the merge bases under
``.rlsbl/bases/`` and the scaffolding version marker ``.rlsbl/version`` -- so
every scaffold of a member produced a workspace failing its own check. The
merge bases now live in the releasable's state directory, under the member's
path, and no marker is written for a member (the release flow writes none for
one either).
"""

import os

from rlsbl.commands.init_cmd import run_cmd, scaffold_bases_dir
from rlsbl.context import create_context
from rlsbl.releasable_cleanup import verify_minimal_rlsbl
from rlsbl.workspace import get_releasable_dir

from test_scaffold_config_skip import _make_explicit_workspace, _setup_releasable

FLAGS = {"auto-commit": False, "auto-tag": False, "skip-shared": False}


def _member(mock_git_repo, monkeypatch):
    proj_dir = mock_git_repo / "app"
    proj_dir.mkdir()
    _make_explicit_workspace(mock_git_repo, [{"name": "www"}], [
        {"path": "app", "name": "app", "releasable": "www"},
    ])
    _setup_releasable(mock_git_repo, "www", config={"publish_mode": "ci"})
    monkeypatch.chdir(proj_dir)
    return proj_dir


def _scaffold(proj_dir):
    run_cmd("plain", [], dict(FLAGS), ctx=create_context(proj_dir))


def test_a_member_scaffold_leaves_no_release_state_in_the_member(
    mock_git_repo, monkeypatch,
):
    proj_dir = _member(mock_git_repo, monkeypatch)
    _scaffold(proj_dir)

    assert not (proj_dir / ".rlsbl" / "bases").exists()
    assert not (proj_dir / ".rlsbl" / "version").exists()
    assert verify_minimal_rlsbl(str(proj_dir)) == []


def test_the_member_s_merge_bases_live_in_the_releasable_state_directory(
    mock_git_repo, monkeypatch,
):
    proj_dir = _member(mock_git_repo, monkeypatch)
    _scaffold(proj_dir)

    expected = os.path.join(
        get_releasable_dir(str(mock_git_repo), "www"), "bases", "app",
    )
    assert os.path.realpath(scaffold_bases_dir(str(proj_dir))) == os.path.realpath(expected)
    stored = sorted(
        os.path.relpath(os.path.join(d, f), expected)
        for d, _dirs, files in os.walk(expected) for f in files
    )
    assert stored, "the scaffold stored no merge base at the releasable level"
    for rel in stored:
        assert (proj_dir / rel).is_file(), f"base {rel} names no managed file"


def test_a_second_scaffold_merges_from_the_releasable_level_bases(
    mock_git_repo, monkeypatch, capsys,
):
    proj_dir = _member(mock_git_repo, monkeypatch)
    _scaffold(proj_dir)
    capsys.readouterr()

    # managed-files.json exists now; a missing bases directory would be refused.
    _scaffold(proj_dir)
    out = capsys.readouterr().out
    assert "CONFLICT" not in out
    assert verify_minimal_rlsbl(str(proj_dir)) == []


def test_a_standalone_project_keeps_its_own_bases_and_marker(mock_git_repo, monkeypatch):
    monkeypatch.chdir(mock_git_repo)
    _scaffold(mock_git_repo)
    assert (mock_git_repo / ".rlsbl" / "bases").is_dir()
    assert (mock_git_repo / ".rlsbl" / "version").is_file()
    assert scaffold_bases_dir(str(mock_git_repo)) == os.path.join(
        str(mock_git_repo), ".rlsbl", "bases")
