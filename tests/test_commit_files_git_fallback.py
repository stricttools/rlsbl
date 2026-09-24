"""``commit_files`` without safegit commits an already-staged deletion.

saferm stages the removal of a tracked file in the git index by default. When
the commit tool is plain git (no safegit on the machine, as on CI runners),
``commit_files`` ran ``git add`` over every named path, and ``git add`` of a
path that is gone from both the working tree and the index is a fatal
"pathspec did not match" -- so an absorb's re-run, whose scaffold had removed
a member's redundant config through saferm, failed on CI only.
"""

import subprocess

from rlsbl import utils


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def test_a_staged_deletion_is_committed_with_the_other_named_files(
    tmp_path, monkeypatch,
):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "gone.json").write_text("{}\n")
    (repo / "kept.txt").write_text("one\n")
    _git(repo, "add", "gone.json", "kept.txt")
    _git(repo, "commit", "-q", "-m", "init")

    # What saferm does by default: remove the file and stage the removal.
    _git(repo, "rm", "-q", "--cached", "gone.json")
    (repo / "gone.json").unlink()
    (repo / "kept.txt").write_text("two\n")

    monkeypatch.setattr(utils, "find_commit_tool", lambda: "git")
    utils.commit_files("step output", ["gone.json", "kept.txt"], cwd=str(repo))

    assert _git(repo, "status", "--porcelain") == ""
    changed = _git(repo, "show", "--name-status", "--format=", "HEAD").split()
    assert changed == ["D", "gone.json", "M", "kept.txt"]


def test_an_unstaged_deletion_named_by_absolute_path_is_still_added(
    tmp_path, monkeypatch,
):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "gone.json").write_text("{}\n")
    _git(repo, "add", "gone.json")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "gone.json").unlink()  # deleted, not staged

    monkeypatch.setattr(utils, "find_commit_tool", lambda: "git")
    utils.commit_files("drop it", [str(repo / "gone.json")], cwd=str(repo))

    assert _git(repo, "status", "--porcelain") == ""


def test_a_removed_directory_named_as_a_path_is_still_added(tmp_path, monkeypatch):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "state").mkdir()
    (repo / "state" / "a.json").write_text("{}\n")
    _git(repo, "add", "state/a.json")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "state" / "a.json").unlink()
    (repo / "state").rmdir()

    monkeypatch.setattr(utils, "find_commit_tool", lambda: "git")
    utils.commit_files("drop the directory", ["state"], cwd=str(repo))

    assert _git(repo, "status", "--porcelain") == ""
