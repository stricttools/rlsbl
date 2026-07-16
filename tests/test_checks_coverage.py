"""Tests for check handler functions to increase code coverage.

Covers:
- checks/release.py: local-tag, remote-tag, github-release, branch-sync
- checks/changelog.py: all 9 changelog check handlers (standalone mode)
- checks/prepush.py: explicit releasable mode paths
- commands/monorepo/batch_release.py: _releasable_release_order, _finalize_batch_file
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import git_head, make_commit, make_ctx, make_workspace, run_git

from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.changelog.schema import ChangelogEntry, serialize_entry


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _setup_changelog_repo(repo, tag="v0.1.0", targets=None):
    """Set up a git repo with .rlsbl/changes/ and a tag.

    Args:
        targets: list of target names to declare in config.json.
            Defaults to [] (explicitly no targets).
    """
    if targets is None:
        targets = []
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")
    if tag:
        run_git(repo, "tag", tag)

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": targets}) + "\n"
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold")


def _write_changelog_md(repo, version, content="Some changes"):
    """Write a CHANGELOG.md with an entry for the given version."""
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n\n{content}\n")


# ==================================================================
# checks/release.py
# ==================================================================


class TestLocalTagCheck:
    """Tests for the local-tag check."""

    def test_tag_exists_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "pass"
        assert "v0.1.0" in result.message

    def test_tag_missing_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None, targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "9.9.9"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "warn"
        assert "not found" in result.message

    def test_no_version_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)
        # No target files -> no version detected

        ctx = make_ctx(repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "skip"


class TestRemoteTagCheck:
    """Tests for the remote-tag check."""

    def test_no_version_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        result = app._check_defs["remote-tag"].impl(ctx)
        assert result.status == "skip"

    def test_local_tag_missing_skips(self, tmp_path, monkeypatch):
        """remote-tag skips when the local tag doesn't exist yet."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None, targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        result = app._check_defs["remote-tag"].impl(ctx)
        assert result.status == "skip"
        assert "not created yet" in result.message

    def test_remote_tag_not_found_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        # No remote exists, so ls-remote will fail
        with patch("rlsbl.utils.run", side_effect=lambda cmd, args, **kw: (
            "" if cmd == "git" and args and args[0] == "ls-remote" else
            subprocess.run([cmd] + (args or []), capture_output=True, text=True, check=True, cwd=kw.get("cwd")).stdout.strip()
        )):
            result = app._check_defs["remote-tag"].impl(ctx)
        assert result.status == "warn"

    def test_remote_tag_found_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="abc123 refs/tags/v0.1.0"):
            result = app._check_defs["remote-tag"].impl(ctx)
        assert result.status == "pass"


class TestGithubReleaseCheck:
    """Tests for the github-release check."""

    def test_no_version_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "skip"

    def test_local_tag_missing_skips(self, tmp_path, monkeypatch):
        """github-release skips when the local tag doesn't exist yet (even without gh)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None, targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        # No gh mocking needed -- the check should skip before reaching gh calls
        with patch("rlsbl.utils.check_gh_installed", side_effect=AssertionError("should not be called")):
            result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "skip"
        assert "not created yet" in result.message

    def test_gh_not_installed_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.check_gh_installed", return_value=False):
            result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "fail"
        assert "not installed" in result.message

    def test_gh_not_authenticated_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.utils.check_gh_installed", return_value=True),
            patch("rlsbl.utils.check_gh_auth", return_value=False),
        ):
            result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "fail"
        assert "not authenticated" in result.message

    def test_release_exists_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.utils.check_gh_installed", return_value=True),
            patch("rlsbl.utils.check_gh_auth", return_value=True),
            patch("rlsbl.utils.run_gh", return_value="release info"),
        ):
            result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "pass"

    def test_release_not_found_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")

        ctx = make_ctx(repo)
        with (
            patch("rlsbl.utils.check_gh_installed", return_value=True),
            patch("rlsbl.utils.check_gh_auth", return_value=True),
            patch("rlsbl.utils.run_gh", side_effect=subprocess.CalledProcessError(1, "gh")),
        ):
            result = app._check_defs["github-release"].impl(ctx)
        assert result.status == "warn"
        assert "not found" in result.message


class TestBranchSyncCheck:
    """Tests for the branch-sync check."""

    def test_no_remote_tracking_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        # No remote => rev-list will fail
        result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "warn"
        assert "no remote tracking" in result.message

    def test_in_sync_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="0\t0"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "pass"

    def test_ahead_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="0\t3"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "warn"
        assert "3 commit(s) ahead" in result.message

    def test_behind_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="2\t0"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "fail"
        assert "2 commit(s) behind" in result.message

    def test_diverged_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="2\t3"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "fail"
        assert "behind" in result.message and "ahead" in result.message

    def test_unexpected_output_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)

        ctx = make_ctx(repo)
        with patch("rlsbl.utils.run", return_value="garbage"):
            result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "warn"
        assert "unexpected" in result.message


# ==================================================================
# checks/changelog.py
# ==================================================================


@pytest.fixture
def changelog_repo(tmp_path, monkeypatch):
    """A git repo with changelog setup, a tag, and a commit with an entry."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    # Include pyproject.toml in initial commit (before the tag)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.1.0"\n'
    )
    run_git(repo, "add", "README.md")
    run_git(repo, "add", "pyproject.toml")
    run_git(repo, "commit", "-q", "-m", "initial")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci"}) + "\n"
    )
    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold")

    # Tag after scaffold
    run_git(repo, "tag", "v0.1.0")

    # Make a real commit that we can add to the changelog
    (repo / "src.py").write_text("x = 1\n")
    run_git(repo, "add", "src.py")
    run_git(repo, "commit", "-q", "-m", "add src")
    head = git_head(repo)

    # Write an entry covering that commit
    entry = json.dumps({
        "commits": [head],
        "user_facing": True,
        "description": "new feature",
        "type": "feature",
    })
    (changes / "unreleased.jsonl").write_text(entry + "\n")
    run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    run_git(repo, "commit", "-q", "-m", "add changelog entry")

    return repo


class TestChangelogEntryCheck:
    """Tests for the changelog-entry check."""

    def test_no_version_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag=None)
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "skip"

    def test_no_changelog_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        run_git(repo, "add", "pyproject.toml")
        run_git(repo, "commit", "-q", "-m", "add pyproject")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "warn"
        assert "not found" in result.message

    def test_entry_exists_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        _write_changelog_md(repo, "0.1.0")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add files")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "pass"

    def test_entry_missing_for_version_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0", targets=["pypi"])
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.1.0"\n'
        )
        _write_changelog_md(repo, "0.0.1")  # wrong version
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "add files")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "warn"
        assert "no entry" in result.message


class TestChangelogHashesCheck:
    """Tests for the changelog-hashes check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "skip"

    def test_valid_hashes_pass(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "pass"

    def test_invalid_hashes_fail(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        entry = json.dumps({
            "commits": ["deadbeefdeadbeef"],
            "user_facing": True,
            "description": "bad",
            "type": "fix",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "bad entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "fail"


class TestChangelogRangeCheck:
    """Tests for the changelog-range check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "skip"

    def test_in_range_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "pass"

    def test_out_of_range_fails(self, tmp_path, monkeypatch):
        """A hash that resolves but is before the tag should fail."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        old_sha = git_head(repo)  # commit BEFORE the tag

        # Tag after the initial commit
        run_git(repo, "tag", "v0.1.0")

        # Create changelog infra
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )

        # Make an entry pointing to the OLD commit (before the tag)
        entry = json.dumps({
            "commits": [old_sha],
            "user_facing": True,
            "description": "old commit",
            "type": "fix",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "add entry for old commit")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-range"].impl(ctx)
        assert result.status == "fail"


class TestChangelogCoverageCheck:
    """Tests for the changelog-coverage check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "skip"

    def test_all_covered_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_uncovered_commit_fails(self, changelog_repo):
        # Add a new commit not covered by any entry
        (changelog_repo / "uncovered.py").write_text("y = 2\n")
        run_git(changelog_repo, "add", "uncovered.py")
        run_git(changelog_repo, "commit", "-q", "-m", "uncovered change")

        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-coverage"].impl(ctx)
        assert result.status == "fail"


class TestChangelogOrphansCheck:
    """Tests for the changelog-orphans check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "skip"

    def test_no_orphans_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "pass"

    def test_all_bad_hashes_orphan_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        entry = json.dumps({
            "commits": ["aaaa" * 10],
            "user_facing": True,
            "description": "orphan",
            "type": "fix",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "orphan entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-orphans"].impl(ctx)
        assert result.status == "fail"


class TestChangelogSchemaCheck:
    """Tests for the changelog-schema check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "skip"

    def test_valid_schema_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "pass"

    def test_bad_schema_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # user_facing: true but no description and no type
        entry = json.dumps({
            "commits": ["abc1234"],
            "user_facing": True,
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "bad schema entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "fail"


class TestChangelogUserFacingCheck:
    """Tests for the changelog-user-facing check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "skip"

    def test_has_user_facing_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "pass"

    def test_no_user_facing_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": False,
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "non-uf entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-user-facing"].impl(ctx)
        assert result.status == "warn"


class TestChangelogBatchCommitsCheck:
    """Tests for the changelog-batch-commits check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "skip"

    def test_within_limit_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "pass"

    def test_exceeds_limit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # Create many commits and reference them all in one entry
        shas = []
        for i in range(10):
            (repo / f"f{i}.py").write_text(f"x{i}\n")
            run_git(repo, "add", f"f{i}.py")
            run_git(repo, "commit", "-q", "-m", f"change {i}")
            shas.append(git_head(repo))

        entry = json.dumps({
            "commits": shas,
            "user_facing": True,
            "description": "big batch",
            "type": "feature",
        })
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "batch entry")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-commits"].impl(ctx)
        assert result.status == "fail"


class TestChangelogBatchEntriesCheck:
    """Tests for the changelog-batch-entries check."""

    def test_no_changes_dir_skips(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "skip"

    def test_within_limit_passes(self, changelog_repo):
        ctx = make_ctx(changelog_repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "pass"

    def test_exceeds_limit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _setup_changelog_repo(repo, tag="v0.1.0")

        # Make one commit
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "change")
        sha = git_head(repo)

        # Reference the same commit in many entries (exceed max_entries_per_commit)
        entries = []
        for i in range(10):
            entries.append(json.dumps({
                "commits": [sha],
                "user_facing": True,
                "description": f"entry {i}",
                "type": "feature",
            }))
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text(
            "\n".join(entries) + "\n"
        )
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "many entries")

        ctx = make_ctx(repo)
        result = app._check_defs["changelog-batch-entries"].impl(ctx)
        assert result.status == "fail"


# ==================================================================
# commands/monorepo/batch_release.py: _releasable_release_order
# ==================================================================


class TestReleasableReleaseOrder:
    """Tests for _releasable_release_order."""

    def test_orders_by_member_topological_position(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order
        from rlsbl.workspace import Releasable, WorkspaceProject

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "init")

        # Create projects: b depends on a
        (repo / "a").mkdir()
        (repo / "a" / "pyproject.toml").write_text(
            '[project]\nname = "a"\nversion = "0.1.0"\n'
        )
        (repo / "b").mkdir()
        (repo / "b" / "pyproject.toml").write_text(
            '[project]\nname = "b"\nversion = "0.1.0"\n'
            '[project.optional-dependencies]\n'
        )

        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "rel-a"}),
            WorkspaceProject({"name": "b", "path": "b", "releasable": "rel-b", "deps": ["a"]}),
        ]

        # Write workspace.toml
        ws_dir = repo / ".rlsbl-monorepo"
        ws_dir.mkdir()
        (ws_dir / "workspace.toml").write_text(
            '[[releasables]]\nname = "rel-a"\n\n'
            '[[releasables]]\nname = "rel-b"\n\n'
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = "rel-a"\n\n'
            '[[projects]]\npath = "b"\nname = "b"\nreleasable = "rel-b"\ndeps = ["a"]\n'
        )
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "setup")

        from rlsbl.workspace_graph import WorkspaceGraph

        releasables = [
            Releasable("rel-a"),
            Releasable("rel-b"),
        ]
        graph = WorkspaceGraph(str(repo), projects)

        result = _releasable_release_order(
            {"rel-a", "rel-b"}, releasables, projects, graph,
        )
        # rel-a should come before rel-b since b depends on a
        assert result.index("rel-a") < result.index("rel-b")

    def test_single_releasable(self):
        """A single releasable returns a list with just that name."""
        from rlsbl.commands.monorepo.batch_release import _releasable_release_order
        from rlsbl.workspace import Releasable, WorkspaceProject

        # Create a mock graph with a simple topological_order
        class MockGraph:
            def topological_order(self):
                return ["a"]

        projects = [
            WorkspaceProject({"name": "a", "path": "a", "releasable": "rel-a"}),
        ]
        releasables = [Releasable("rel-a")]

        result = _releasable_release_order(
            {"rel-a"}, releasables, projects, MockGraph(),
        )
        assert result == ["rel-a"]


# ==================================================================
# commands/monorepo/batch_release.py: _finalize_batch_file
# ==================================================================


class TestFinalizeBatchFile:
    """Tests for _finalize_batch_file."""

    def test_renames_and_locks(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.batch_release import _finalize_batch_file

        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()
        batch_path = str(releases_dir / "unreleased.toml")

        with open(batch_path, "w") as f:
            f.write("[packages.test]\nbump = 'patch'\n")

        log_messages = []

        with patch("rlsbl.commands.monorepo.batch_release.commit_files"):
            _finalize_batch_file(batch_path, log_messages.append)

        # Original path should no longer exist (renamed, not recreated)
        assert not os.path.exists(batch_path)

        # A timestamped batch-*.toml should exist
        toml_files = [f for f in os.listdir(str(releases_dir)) if f.startswith("batch-")]
        assert len(toml_files) == 1

        # Timestamped file should be read-only (444)
        ts_path = str(releases_dir / toml_files[0])
        mode = os.stat(ts_path).st_mode
        assert mode & stat.S_IWUSR == 0  # not writable

        assert any("Finalized" in m for m in log_messages)


# ==================================================================
# checks/prepush.py: monorepo without explicit releasables path
# ==================================================================


class TestPrepushChangelogCoverageMonorepoNoReleasables:
    """Tests for prepush-changelog-coverage in monorepo without [[releasables]]."""

    def test_monorepo_uncovered_commit_fails(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        # Create a sub-project with changelog
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "alpha@v0.1.0")

        base_sha = git_head(repo)

        # Make an uncovered commit
        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: uncovered")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"

    def test_monorepo_covered_commit_passes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        run_git(repo, "tag", "alpha@v0.1.0")

        base_sha = git_head(repo)

        # Make a commit and cover it
        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: covered")
        head_sha = git_head(repo)

        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", "packages/alpha/.rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "add entry")

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = f"refs/heads/main {git_head(repo)} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_monorepo_non_releasable_skipped(self, tmp_path, monkeypatch):
        """Non-releasable (dev_node) projects are skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        # dev_node project -- not releasable
        pkg = repo / "packages" / "devtool"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "devtool", "version": "0.1.0"}\n')

        make_workspace(
            repo,
            [{"path": "packages/devtool", "name": "devtool", "dev_node": True}],
        )
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/devtool/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: devtool change")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        # dev_node is non-releasable -> skipped -> pass (all affected covered)
        assert result.status == "pass"

    def test_single_project_no_changes_dir_skips(self, tmp_path, monkeypatch):
        """Single-project mode with no .rlsbl/changes/ skips."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "change")
        head = git_head(repo)

        # No .rlsbl/changes/ dir
        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"
        assert "not set up" in result.message

    def test_single_project_covered_passes(self, tmp_path, monkeypatch):
        """Single-project mode with covered commits passes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "change")
        head = git_head(repo)

        entry = json.dumps({
            "commits": [head],
            "user_facing": True,
            "description": "feat",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")
        run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
        run_git(repo, "commit", "-q", "-m", "add entry")

        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {git_head(repo)} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"

    def test_single_project_uncovered_fails(self, tmp_path, monkeypatch):
        """Single-project mode with uncovered commits fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")
        run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (repo / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci"}))
        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold")
        base = git_head(repo)

        (repo / "g.txt").write_text("y\n")
        run_git(repo, "add", "g.txt")
        run_git(repo, "commit", "-q", "-m", "uncovered change")
        head = git_head(repo)

        ctx = make_ctx(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {base}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"

    def test_manual_warning_no_push_context_skips(self, tmp_path, monkeypatch):
        """prepush-manual-warning skips when push_stdin is None."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x\n")
        run_git(repo, "add", "f.txt")
        run_git(repo, "commit", "-q", "-m", "init")

        ctx = make_ctx(repo)
        assert ctx.push_stdin is None
        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "skip"
        assert "not in push context" in result.message

    def test_monorepo_no_pushed_commits_skips(self, tmp_path, monkeypatch):
        """Monorepo mode skips when pushed commits can't be determined."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@t.t")
        run_git(repo, "config", "user.name", "T")
        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')
        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        # Use zero-sha as remote ref to simulate a "can't determine" path
        zero = "0" * 40
        head = git_head(repo)
        ctx.push_stdin = f"refs/heads/main {head} refs/heads/main {zero}"

        # Patch get_push_changed_files to return None
        with patch("rlsbl.git_util.get_push_changed_files", return_value=None):
            result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"

    def test_no_affected_projects_passes(self, tmp_path, monkeypatch):
        """Commits not touching any project pass."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold")

        base_sha = git_head(repo)

        # Make a commit NOT in any project path (root-level)
        (repo / "root-file.txt").write_text("root change\n")
        run_git(repo, "add", "root-file.txt")
        run_git(repo, "commit", "-q", "-m", "root change")
        head_sha = git_head(repo)

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"
        assert "no affected" in result.message
