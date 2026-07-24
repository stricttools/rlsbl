"""Tests for rlsbl.utils.remote_tag_commit — commit-aware remote tag resolution.

Drives against real git: a working repo plus a file:// bare remote in tmp_path.
Verifies PRESENT returns the correct peeled commit SHA for BOTH lightweight and
annotated tags (annotated is the peel case a naive first-line compare would
false-negative), ABSENT after tag deletion, and INCONCLUSIVE for a nonexistent
remote path.
"""

from githarness import add_remote, commit_file, git, init_repo

from rlsbl.utils import RemoteTagState, remote_tag_commit


def _setup(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    head = commit_file(repo, "README.md", "# hi\n", "initial")
    add_remote(repo, tmp_path / "remote")
    return repo, head


def test_present_lightweight_tag(tmp_path):
    repo, head = _setup(tmp_path)
    git(repo, "tag", "v1.0.0")
    git(repo, "push", "-q", "origin", "v1.0.0")

    result = remote_tag_commit("v1.0.0", cwd=str(repo))
    assert result.state is RemoteTagState.PRESENT
    assert result.commit == head
    assert result.error is None


def test_present_annotated_tag_peels_to_commit(tmp_path):
    repo, head = _setup(tmp_path)
    # Annotated tag: ls-remote emits the tag-object SHA on the direct line and
    # the commit SHA on the ^{} peeled line. The commit must be the peeled one.
    git(repo, "tag", "-a", "v2.0.0", "-m", "release 2.0.0")
    git(repo, "push", "-q", "origin", "v2.0.0")

    tag_object_sha = git(repo, "rev-parse", "v2.0.0")
    assert tag_object_sha != head  # annotated tag object is distinct from commit

    result = remote_tag_commit("v2.0.0", cwd=str(repo))
    assert result.state is RemoteTagState.PRESENT
    assert result.commit == head
    assert result.commit != tag_object_sha
    assert result.error is None


def test_absent_after_delete(tmp_path):
    repo, _ = _setup(tmp_path)
    git(repo, "tag", "v3.0.0")
    git(repo, "push", "-q", "origin", "v3.0.0")
    git(repo, "push", "-q", "origin", ":refs/tags/v3.0.0")  # delete on remote

    result = remote_tag_commit("v3.0.0", cwd=str(repo))
    assert result.state is RemoteTagState.ABSENT
    assert result.commit is None
    assert result.error is None


def test_absent_never_pushed(tmp_path):
    repo, _ = _setup(tmp_path)
    result = remote_tag_commit("v9.9.9", cwd=str(repo))
    assert result.state is RemoteTagState.ABSENT
    assert result.commit is None


def test_inconclusive_nonexistent_remote(tmp_path):
    repo, _ = _setup(tmp_path)
    missing = tmp_path / "does-not-exist.git"
    result = remote_tag_commit("v1.0.0", cwd=str(repo), remote=str(missing))
    assert result.state is RemoteTagState.INCONCLUSIVE
    assert result.commit is None
    assert result.error  # underlying git error text is carried
