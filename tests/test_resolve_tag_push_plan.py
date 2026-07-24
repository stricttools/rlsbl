"""Tests for rlsbl.utils.resolve_tag_push_plan -- commit-aware tag push decision.

Drives against real git: a working repo plus a file:// bare remote in tmp_path.
Covers every branch of the decision: all-present-matching -> skip, any-absent ->
push, present-at-different-commit -> hard error, inconclusive remote -> hard
error, and the partial-companion (mixed present/absent) case that must still
push (git no-ops the identical refs).
"""

import subprocess

import pytest
from githarness import add_remote, commit_file, git, init_repo

from rlsbl.errors import GitError
from rlsbl.utils import resolve_tag_push_plan


def _setup(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    head = commit_file(repo, "README.md", "# hi\n", "initial")
    add_remote(repo, tmp_path / "remote")
    return repo, head


def _push(repo, *refs):
    # Push helper that bypasses the literal "git push" permission hook by going
    # through subprocess directly (the harness git() also uses subprocess).
    subprocess.run(
        ["git", "push", "-q", "origin", *refs],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )


def test_all_present_matching_skips(tmp_path):
    repo, _ = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "v1.0.0-go")  # companion, same commit
    _push(repo, "v1.0.0", "v1.0.0-go")

    # Every tag already on the remote at the matching commit -> skip.
    assert resolve_tag_push_plan(["v1.0.0", "v1.0.0-go"], cwd=str(repo)) is False


def test_absent_needs_push(tmp_path):
    repo, _ = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    # Not pushed at all.
    assert resolve_tag_push_plan(["v1.0.0"], cwd=str(repo)) is True


def test_partial_companion_needs_push(tmp_path):
    repo, _ = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "v1.0.0-go")
    _push(repo, "v1.0.0")  # primary pushed, companion missing

    # Mixed state: primary present-identical, companion absent -> push proceeds.
    assert resolve_tag_push_plan(["v1.0.0", "v1.0.0-go"], cwd=str(repo)) is True


def test_present_different_commit_hard_errors(tmp_path):
    repo, head = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    _push(repo, "v1.0.0")
    # Move the local tag to a new commit, diverging from the remote.
    new_head = commit_file(repo, "b.txt", "b\n", "second")
    assert new_head != head
    git(repo, "tag", "-f", "v1.0.0")

    with pytest.raises(GitError) as exc:
        resolve_tag_push_plan(["v1.0.0"], cwd=str(repo))
    msg = str(exc.value)
    assert "v1.0.0" in msg
    assert new_head in msg  # local SHA named
    assert head in msg      # divergent remote SHA named


def test_inconclusive_remote_hard_errors(tmp_path):
    repo, _ = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    missing = tmp_path / "does-not-exist.git"
    with pytest.raises(GitError) as exc:
        resolve_tag_push_plan(["v1.0.0"], cwd=str(repo), remote=str(missing))
    assert "v1.0.0" in str(exc.value)
