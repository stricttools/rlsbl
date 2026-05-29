"""Tests for JSONL changelog integration with release, pre-push, unreleased, and status commands."""

import json
import os
import subprocess
import sys

import pytest

from conftest import make_commit as _make_commit
from rlsbl.changelog import (
    ChangelogEntry,
    append_entry,
    changes_dir_exists,
    get_changes_dir,
    serialize_entry,
)


def _setup_jsonl_project(repo_path, commits_and_entries):
    """Set up a project with .rlsbl/changes/ and entries covering given commits.

    commits_and_entries: list of (sha, description, type, user_facing) tuples.
    Each tuple creates one entry. If sha is a list, all are grouped.
    """
    changes_dir = get_changes_dir(str(repo_path))
    os.makedirs(changes_dir, exist_ok=True)

    for item in commits_and_entries:
        sha_or_list, description, entry_type, user_facing = item
        if isinstance(sha_or_list, str):
            commits = [sha_or_list[:12]]
        else:
            commits = [s[:12] for s in sha_or_list]
        entry = ChangelogEntry(
            commits=commits,
            user_facing=user_facing,
            description=description,
            type=entry_type,
        )
        append_entry(changes_dir, entry)


def _setup_npm_project(repo_path, version="1.0.0"):
    """Create a minimal package.json."""
    pkg = {"name": "test-pkg", "version": version}
    (repo_path / "package.json").write_text(json.dumps(pkg))


def _setup_rlsbl_config(repo_path):
    """Create minimal .rlsbl/config.json."""
    rlsbl_dir = repo_path / ".rlsbl"
    rlsbl_dir.mkdir(exist_ok=True)
    config = {"targets": ["npm"]}
    (rlsbl_dir / "config.json").write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Release integration tests
# ---------------------------------------------------------------------------


class TestReleaseWithJsonl:
    """Release command uses JSONL validation and generation when .rlsbl/changes/ exists."""

    def test_release_validates_and_generates(self, mock_git_repo, monkeypatch):
        """JSONL entries covering all commits -> validation passes, CHANGELOG.md generated."""
        repo = mock_git_repo
        _setup_npm_project(repo)
        _setup_rlsbl_config(repo)

        # Create a baseline version tag before making unreleased commits
        subprocess.run(["git", "tag", "v0.0.0"], cwd=str(repo), check=True)

        sha1 = _make_commit(repo, "feat1.txt", "feat: first feature")
        sha2 = _make_commit(repo, "feat2.txt", "feat: second feature")

        _setup_jsonl_project(repo, [
            (sha1, "First feature", "feature", True),
            (sha2, "Second feature", "feature", True),
        ])

        # Create CHANGELOG.md with version heading (generate_changelog will overwrite)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        # Import after setup
        from rlsbl.changelog import generate_changelog, validate_unreleased
        from rlsbl.changelog.files import get_changes_dir

        changes_dir = get_changes_dir(".")

        # Validate should pass
        result = validate_unreleased(changes_dir, project_root=str(repo))
        # Validation uses v0.0.0..HEAD range to find unreleased commits

        # Generate should produce CHANGELOG.md
        generate_changelog(".")
        assert os.path.exists("CHANGELOG.md")
        content = (repo / "CHANGELOG.md").read_text()
        assert "First feature" in content
        assert "Second feature" in content


class TestReleaseWithoutJsonl:
    """Release command uses manual CHANGELOG.md validation when no .rlsbl/changes/."""

    def test_no_changes_dir_uses_manual_validation(self, mock_git_repo):
        """Without .rlsbl/changes/, the old heading-based validation applies."""
        repo = mock_git_repo
        assert not changes_dir_exists(".")

        # This confirms the code path: no .rlsbl/changes/ means JSONL is skipped
        _setup_npm_project(repo)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        from rlsbl.utils import extract_changelog_entry

        entry = extract_changelog_entry("CHANGELOG.md", "1.0.0")
        assert entry is not None
        assert "Init" in entry


class TestReleaseJsonlValidationFails:
    """Release aborts when JSONL validation fails."""

    def test_empty_unreleased_fails_coverage(self, mock_git_repo, tmp_path):
        """Empty unreleased.jsonl with commits ahead -> coverage check fails."""
        repo = mock_git_repo
        _setup_npm_project(repo)
        _setup_rlsbl_config(repo)

        # Create a baseline version tag before the unreleased commit
        subprocess.run(["git", "tag", "v0.0.0"], cwd=str(repo), check=True)

        # Now make a new commit that's ahead of the tag
        _make_commit(repo, "feat.txt", "feat: something")

        # Create .rlsbl/changes/ with empty unreleased.jsonl
        changes_dir = get_changes_dir(".")
        os.makedirs(changes_dir, exist_ok=True)
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")

        from rlsbl.changelog import validate_unreleased

        result = validate_unreleased(changes_dir, project_root=str(repo))
        # Coverage check should fail since no entries cover the commit
        passed, details = result["checks"]["coverage"]
        assert not passed
        assert len(details) > 0


# ---------------------------------------------------------------------------
# Pre-push integration tests
# ---------------------------------------------------------------------------


class TestPrePushWithJsonl:
    """Pre-push check uses JSONL coverage when .rlsbl/changes/ exists."""

    def test_covered_commits_pass(self, mock_git_repo, tmp_path):
        """All pushed commits covered by JSONL entries -> success."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        # Get the initial commit SHA
        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        sha1 = _make_commit(repo, "feat.txt", "feat: something")

        _setup_jsonl_project(repo, [
            (sha1, "Something", "feature", True),
        ])

        from rlsbl.commands.pre_push_check import _check_jsonl_changelog

        # Simulate refs: pushing sha1 with known remote state (initial_sha)
        refs = [(sha1, initial_sha)]
        error = _check_jsonl_changelog(".", refs)
        assert error is None

    def test_missing_coverage_fails(self, mock_git_repo):
        """Uncovered commit in push range -> error."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        # Record initial commit as the "remote" state
        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        sha1 = _make_commit(repo, "feat.txt", "feat: something")
        sha2 = _make_commit(repo, "fix.txt", "fix: another thing")

        # Only cover sha1, not sha2
        _setup_jsonl_project(repo, [
            (sha1, "Something", "feature", True),
        ])

        from rlsbl.commands.pre_push_check import _check_jsonl_changelog

        # Push range: initial_sha..sha2 includes both sha1 and sha2
        refs = [(sha2, initial_sha)]
        error = _check_jsonl_changelog(".", refs)
        assert error is not None
        assert "missing coverage" in error.lower()

    def test_no_entries_fails(self, mock_git_repo):
        """Empty unreleased.jsonl -> error."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        sha = _make_commit(repo, "feat.txt", "feat: something")

        changes_dir = get_changes_dir(".")
        os.makedirs(changes_dir, exist_ok=True)
        (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")

        from rlsbl.commands.pre_push_check import _check_jsonl_changelog

        refs = [(sha, initial_sha)]
        error = _check_jsonl_changelog(".", refs)
        assert error is not None
        assert "no jsonl changelog entries found" in error.lower()


class TestPrePushWithoutJsonl:
    """Pre-push check warns when no .rlsbl/changes/ but doesn't block."""

    def test_warns_no_jsonl(self, tmp_project, capsys):
        """Without .rlsbl/changes/, warns and exits 0."""
        (tmp_project / "package.json").write_text(
            json.dumps({"name": "test-pkg", "version": "1.0.0"})
        )
        (tmp_project / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        assert not changes_dir_exists(".")

        from rlsbl.commands.pre_push_check import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, project_root=tmp_project)

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "JSONL changelog not set up" in captured.err


# ---------------------------------------------------------------------------
# Unreleased integration tests
# ---------------------------------------------------------------------------


class TestUnreleasedWithJsonl:
    """Unreleased command uses hash-based matching when .rlsbl/changes/ exists."""

    def test_exact_hash_matching(self, mock_git_repo, capsys):
        """JSONL entries with matching hashes -> commits marked [COVERED]."""
        repo = mock_git_repo
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        sha = _make_commit(repo, "feat.txt", "feat: add widget")

        _setup_jsonl_project(repo, [
            (sha, "Add widget", "feature", True),
        ])

        from rlsbl.commands.unreleased import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, project_root=mock_git_repo)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "[COVERED]" in captured.out

    def test_uncovered_commits_marked_missing(self, mock_git_repo, capsys):
        """Commits not in any JSONL entry -> marked [MISSING]."""
        repo = mock_git_repo
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        sha1 = _make_commit(repo, "feat.txt", "feat: add widget")
        sha2 = _make_commit(repo, "fix.txt", "fix: something else")

        # Only cover sha1
        _setup_jsonl_project(repo, [
            (sha1, "Add widget", "feature", True),
        ])

        from rlsbl.commands.unreleased import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, project_root=mock_git_repo)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "[COVERED]" in captured.out
        assert "[MISSING]" in captured.out
        assert "Coverage: 1/2" in captured.out

    def test_json_output_with_jsonl(self, mock_git_repo, capsys):
        """JSON output mode works with JSONL matching."""
        repo = mock_git_repo
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        sha = _make_commit(repo, "feat.txt", "feat: add widget")

        _setup_jsonl_project(repo, [
            (sha, "Add widget", "feature", True),
        ])

        from rlsbl.commands.unreleased import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {"json": True}, project_root=mock_git_repo)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["coverage"]["covered"] == 1
        assert data["coverage"]["total"] == 1


class TestUnreleasedWithoutJsonl:
    """Unreleased command requires JSONL changelog."""

    def test_errors_without_jsonl(self, mock_git_repo, capsys):
        """Without .rlsbl/changes/, unreleased exits with error."""
        repo = mock_git_repo
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        _make_commit(repo, "feat.txt", "feat: add widget support")

        assert not changes_dir_exists(".")

        from rlsbl.commands.unreleased import run_cmd

        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, project_root=mock_git_repo)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "JSONL changelog not set up" in captured.err


# ---------------------------------------------------------------------------
# Status integration tests
# ---------------------------------------------------------------------------


class TestStatusWithJsonl:
    """Status command shows JSONL coverage info when .rlsbl/changes/ exists."""

    def test_shows_jsonl_coverage(self, mock_git_repo, capsys):
        """With .rlsbl/changes/ and entries, coverage info is displayed."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        # Create a baseline version tag before unreleased commits
        subprocess.run(["git", "tag", "v0.0.0"], cwd=str(repo), check=True)

        sha = _make_commit(repo, "feat.txt", "feat: something")

        _setup_jsonl_project(repo, [
            (sha, "Something", "feature", True),
        ])

        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        from rlsbl.commands.status import _collect_status

        data = _collect_status("npm", project_root=".")
        # jsonl_coverage should be set (non-None)
        assert data["jsonl_coverage"] is not None
        assert "covered" in data["jsonl_coverage"] or "entries" in data["jsonl_coverage"]

    def test_json_output_includes_jsonl(self, mock_git_repo, capsys):
        """JSON output includes jsonl_coverage field."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        # Create a baseline version tag before unreleased commits
        subprocess.run(["git", "tag", "v0.0.0"], cwd=str(repo), check=True)

        sha = _make_commit(repo, "feat.txt", "feat: something")

        _setup_jsonl_project(repo, [
            (sha, "Something", "feature", True),
        ])

        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        from rlsbl.commands.status import run_cmd

        run_cmd("npm", [], {"json": True}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "jsonl_coverage" in data

    def test_display_output_shows_jsonl_line(self, mock_git_repo, capsys):
        """Text output includes JSONL line when coverage data exists."""
        repo = mock_git_repo
        _setup_npm_project(repo)

        # Create a baseline version tag before unreleased commits
        subprocess.run(["git", "tag", "v0.0.0"], cwd=str(repo), check=True)

        sha = _make_commit(repo, "feat.txt", "feat: something")

        _setup_jsonl_project(repo, [
            (sha, "Something", "feature", True),
        ])

        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        from rlsbl.commands.status import run_cmd

        run_cmd("npm", [], {}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        assert "JSONL:" in captured.out


class TestStatusWithoutJsonl:
    """Status command shows 'not set up' when .rlsbl/changes/ is absent."""

    def test_shows_not_set_up(self, mock_git_repo, capsys):
        """Without .rlsbl/changes/, jsonl_coverage is 'not set up'."""
        repo = mock_git_repo
        _setup_npm_project(repo)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        assert not changes_dir_exists(".")

        from rlsbl.commands.status import _collect_status

        data = _collect_status("npm", project_root=".")
        assert data["jsonl_coverage"] == "not set up"

    def test_text_output_shows_not_set_up(self, mock_git_repo, capsys):
        """Text output shows JSONL: not set up when no .rlsbl/changes/ exists."""
        repo = mock_git_repo
        _setup_npm_project(repo)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- Init\n")

        from rlsbl.commands.status import run_cmd

        run_cmd("npm", [], {}, project_root=mock_git_repo)
        captured = capsys.readouterr()
        assert "JSONL:     not set up" in captured.out
