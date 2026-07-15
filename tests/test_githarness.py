"""Self-tests for the shared real-git test harness (githarness.py)."""

from githarness import (
    add_remote,
    commit_file,
    git,
    init_repo,
    remote_ref,
    snapshot_remote_refs,
)


def test_repo_plus_bare_remote_in_five_lines(tmp_path):
    """The headline ergonomic goal: repo + bare remote in <=5 lines."""
    repo = tmp_path / "repo"
    init_repo(repo)
    head = commit_file(repo, "README.md", "# hi\n", "initial")
    add_remote(repo, tmp_path / "remote")

    # The remote's main branch is in sync with local HEAD after add_remote.
    assert remote_ref(repo, "refs/heads/main") == head


def test_git_returns_stdout_stripped(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "one\n", "first")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch == "main"


def test_git_check_false_swallows_failure(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "one\n", "first")
    # A path that does not exist at HEAD: check=False -> empty string, no raise.
    missing = git(repo, "show", "HEAD:nope.txt", check=False)
    assert missing == ""


def test_init_repo_custom_identity_and_branch(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo, email="who@example.com", name="Who", branch="trunk")
    commit_file(repo, "a.txt", "one\n", "first")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"
    assert git(repo, "config", "user.name") == "Who"
    assert git(repo, "config", "user.email") == "who@example.com"


def test_commit_file_returns_head_and_creates_parents(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    h1 = commit_file(repo, "nested/deep/file.txt", "x\n", "one")
    h2 = commit_file(repo, "nested/deep/file.txt", "y\n", "two")
    assert h1 != h2
    assert git(repo, "rev-parse", "HEAD") == h2
    assert (repo / "nested" / "deep" / "file.txt").read_text() == "y\n"


def test_add_remote_no_push(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "a.txt", "one\n", "first")
    add_remote(repo, tmp_path / "remote", push=False)
    # Nothing pushed yet: remote has no main ref.
    assert remote_ref(repo, "refs/heads/main") == ""


def test_snapshot_remote_refs(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    head = commit_file(repo, "a.txt", "one\n", "first")
    git(repo, "tag", "v1.0.0")
    add_remote(repo, tmp_path / "remote")
    refs = snapshot_remote_refs(repo)
    assert refs["refs/heads/main"] == head
    assert refs["refs/tags/v1.0.0"] == head
