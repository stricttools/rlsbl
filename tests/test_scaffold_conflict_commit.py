"""Tests for the conflict-commit fix: conflicted files (with merge markers)
must be excluded from the scaffold auto-commit.
"""

import os
import subprocess

import pytest

from rlsbl.commands.init_cmd import _finalize_scaffold


def _commit_all(repo, message="setup"):
    subprocess.run(
        ["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo), check=True, capture_output=True,
    )


def _porcelain(repo):
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout


def test_finalize_excludes_conflicted_files_from_commit(mock_git_repo, capsys):
    """A file with status starting with 'CONFLICTS' must NOT be committed."""
    # Pre-create a conflict marker file on disk so _finalize sees it.
    conflict_file = "conflicted.txt"
    (mock_git_repo / conflict_file).write_text(
        "<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n"
    )
    clean_file = "clean.txt"
    (mock_git_repo / clean_file).write_text("clean content\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[
            (conflict_file, "CONFLICTS -- resolve manually"),
            (clean_file, "merged"),
        ],
        skipped=[],
        warnings=[],
        registry=None,
        flags={"no-tag": True},
        registries=[],
        project_root=".",
    )

    captured = capsys.readouterr()
    # The fix should print a warning about the skipped conflicted file.
    assert "Skipped commit for 1 conflicted file" in captured.err
    assert conflict_file in captured.err

    # The conflict file remains uncommitted (still showing in porcelain output).
    porcelain = _porcelain(mock_git_repo)
    assert conflict_file in porcelain, (
        f"Conflicted file must NOT be committed but porcelain is empty: {porcelain!r}"
    )

    # The clean file was committed, so should NOT appear in porcelain.
    assert clean_file not in porcelain, (
        f"Clean file should be committed but porcelain shows: {porcelain!r}"
    )


def test_finalize_handles_only_conflicted_files(mock_git_repo, capsys):
    """If every created file is conflicted, no commit should be attempted."""
    cf = "all-conflict.txt"
    (mock_git_repo / cf).write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=[(cf, "CONFLICTS -- resolve manually")],
        skipped=[],
        warnings=[],
        registry=None,
        flags={"no-tag": True},
        registries=[],
        project_root=".",
    )

    captured = capsys.readouterr()
    # Warning printed
    assert "Skipped commit for 1 conflicted file" in captured.err

    # No commit message printed for "Committed scaffold changes."
    # The internal commit may have happened only for the rlsbl/version marker etc.
    # Conflict file must still be in porcelain.
    porcelain = _porcelain(mock_git_repo)
    assert cf in porcelain


def test_finalize_commits_only_clean_files_when_mix(mock_git_repo, capsys):
    """Mix of conflicted and clean files: only clean ones get committed."""
    files = [
        ("a.txt", "merged"),
        ("b.txt", "CONFLICTS -- resolve manually"),
        ("c.txt", "created"),
        ("d.txt", "CONFLICTS in publish.yml"),  # any CONFLICTS-prefix variant
    ]
    for name, _ in files:
        (mock_git_repo / name).write_text(f"content of {name}\n")

    _finalize_scaffold(
        existing_hashes={},
        all_hash_dicts=[{}],
        created=files,
        skipped=[],
        warnings=[],
        registry=None,
        flags={"no-tag": True},
        registries=[],
        project_root=".",
    )

    captured = capsys.readouterr()
    assert "Skipped commit for 2 conflicted file" in captured.err

    porcelain = _porcelain(mock_git_repo)
    # Clean files committed -- not in porcelain
    assert "a.txt" not in porcelain
    assert "c.txt" not in porcelain
    # Conflicted files NOT committed -- still in porcelain
    assert "b.txt" in porcelain
    assert "d.txt" in porcelain
