"""Tests for the release's commit trail: the commits the release flow itself
created, recorded in its state file so the foreign-commit guards can tell a
concurrent session's commits from the release's own.
"""

import subprocess


from rlsbl.commands.release.execute import _track_release_created_commit
from rlsbl.commands.release.release_state import (
    load_release_state,
    save_release_state,
)


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(repo):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _setup_repo(tmp_path):
    """Create a git repo with an initial commit and return (repo, pre_release_sha)."""
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")

    pre_release_sha = _git_head(repo)

    # Create .rlsbl/releases/ for state file
    releases_dir = repo / ".rlsbl" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    return repo, pre_release_sha


class TestTrackReleaseCreatedCommit:
    """Tests for _track_release_created_commit: captures HEAD SHA into state."""

    def test_tracks_single_commit(self, tmp_path):
        """A single release commit is recorded in state."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        # Save initial state
        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        # Make a release commit
        (repo / "version.txt").write_text("1.0.0\n")
        _git(repo, "add", "version.txt")
        _git(repo, "commit", "-q", "-m", "v1.0.0")
        release_sha = _git_head(repo)

        _track_release_created_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert "release_created_commits" in state
        assert release_sha in state["release_created_commits"]

    def test_tracks_multiple_commits(self, tmp_path):
        """Multiple release commits are all recorded."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        shas = []
        for i in range(3):
            (repo / f"file{i}.txt").write_text(f"content {i}\n")
            _git(repo, "add", f"file{i}.txt")
            _git(repo, "commit", "-q", "-m", f"release commit {i}")
            shas.append(_git_head(repo))
            _track_release_created_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert len(state["release_created_commits"]) == 3
        for sha in shas:
            assert sha in state["release_created_commits"]

    def test_deduplicates_same_sha(self, tmp_path):
        """Calling _track_release_created_commit twice for the same HEAD is idempotent."""
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")

        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        (repo / "file.txt").write_text("data\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-q", "-m", "commit")

        _track_release_created_commit(state_path, cwd=str(repo))
        _track_release_created_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert len(state["release_created_commits"]) == 1

    def test_the_state_key_names_the_commits_the_release_created(self, tmp_path):
        """The trail is the commits the release FLOW created, not release commits.

        "Release commit" is the ruled term for the commit a version shipped
        from -- what an archive's candidate_sha records and what a released tag
        points at. This trail is the opposite kind of thing: the version bump
        and the finalization commits the flow writes on top, which the range
        pin recognizes as its own. Two senses under one spelling is how a
        reader concludes that the trail is the release commit.
        """
        repo, pre_release_sha = _setup_repo(tmp_path)
        state_path = str(repo / ".rlsbl" / "releases" / "in-progress.json")
        save_release_state(state_path, {"pre_release_sha": pre_release_sha})

        (repo / "file.txt").write_text("data\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-q", "-m", "v1.0.0")
        _track_release_created_commit(state_path, cwd=str(repo))

        state = load_release_state(state_path)
        assert "release_created_commits" in state
        assert "release_commits" not in state, (
            "the old spelling collides with the ruled term for the commit a "
            "version shipped from"
        )
