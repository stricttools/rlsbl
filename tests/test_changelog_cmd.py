"""Tests for the changelog add/generate subcommands."""

import json
import os
from unittest import mock

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit
from rlsbl.changelog.files import (
    append_entry,
    get_changes_dir,
    read_unreleased,
)
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.commands.changelog_cmd import cmd_add, cmd_generate


@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """Create a git repo with .rlsbl/ scaffolding and a baseline version tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit
    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    # Create a baseline version tag so <tag>..HEAD works
    _run_git(repo, "tag", "v0.0.0")

    # Set up .rlsbl/changes
    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    return repo


# ---------------------------------------------------------------------------
# cmd_add tests
# ---------------------------------------------------------------------------


class TestCmdAdd:
    """Tests for cmd_add."""

    def test_valid_entry(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha[:12],
            "description": "New feature",
            "type": "feature",
            "user-facing": True,
        }
        cmd_add(flags, project_root=rlsbl_repo)

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 1
        assert entries[0].user_facing is True
        assert entries[0].description == "New feature"
        assert entries[0].type == "feature"
        # Should be resolved to full 40-char hash
        assert len(entries[0].commits[0]) == 40

    def test_non_user_facing(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha,
            "description": "",
            "type": "",
            "user-facing": False,
        }
        cmd_add(flags, project_root=rlsbl_repo)

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 1
        assert entries[0].user_facing is False
        assert entries[0].description is None
        assert entries[0].type is None

    def test_invalid_commit_hash(self, rlsbl_repo):
        flags = {
            "commits": "deadbeefdeadbeef",
            "description": "Stuff",
            "type": "fix",
            "user-facing": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_missing_commits(self, rlsbl_repo):
        flags = {
            "commits": "",
            "description": "Stuff",
            "type": "fix",
            "user-facing": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_missing_description_for_user_facing(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha,
            "description": "",
            "type": "fix",
            "user-facing": True,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_multiple_commits(self, rlsbl_repo):
        sha1 = _make_commit(rlsbl_repo, "a.txt")
        sha2 = _make_commit(rlsbl_repo, "b.txt")
        flags = {
            "commits": f"{sha1[:8]},{sha2[:8]}",
            "description": "Multi-commit change",
            "type": "feature",
            "user-facing": True,
        }
        cmd_add(flags, project_root=rlsbl_repo)

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 1
        assert len(entries[0].commits) == 2

    def test_add_auto_commits(self, rlsbl_repo):
        """After adding an entry, commit_files is called with correct args."""
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha[:12],
            "description": "New feature",
            "type": "feature",
            "user-facing": True,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_add(flags, project_root=rlsbl_repo)
            mock_commit.assert_called_once()
            call_args = mock_commit.call_args
            assert call_args[0][0] == "changelog: New feature"
            unreleased_path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
            assert call_args[0][1] == [unreleased_path]
            assert call_args[1]["allow_failure"] is True

    def test_add_auto_commit_has_autogenerated_trailer(self, rlsbl_repo):
        """The auto-commit from cmd_add passes autogenerated=True."""
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha[:12],
            "description": "New feature",
            "type": "feature",
            "user-facing": True,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_add(flags, project_root=rlsbl_repo)
            mock_commit.assert_called_once()
            assert mock_commit.call_args[1].get("autogenerated", True) is True

    def test_add_no_commit_flag(self, rlsbl_repo):
        """With --no-commit, commit_files is NOT called."""
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha[:12],
            "description": "New feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_add(flags, project_root=rlsbl_repo)
            mock_commit.assert_not_called()

    def test_add_non_user_facing_commit_message(self, rlsbl_repo):
        """Non-user-facing entries use a generic commit message."""
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha,
            "description": "",
            "type": "",
            "user-facing": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_add(flags, project_root=rlsbl_repo)
            mock_commit.assert_called_once()
            assert mock_commit.call_args[0][0] == "changelog: non-user-facing entry"

    def test_duplicate_commit_same_type_rejected(self, rlsbl_repo):
        """Adding the same commit with the same user_facing and type is a hard error."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha[:12],
            "description": "First feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)

        flags_second = {
            "commits": sha[:12],
            "description": "Duplicate feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags_second, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 1

    def test_duplicate_commit_different_type_allowed(self, rlsbl_repo):
        """Same commit with different types is allowed (one commit, two changelog types)."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha[:12],
            "description": "Feature entry",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)

        flags_second = {
            "commits": sha[:12],
            "description": "Fix entry",
            "type": "fix",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_second, project_root=rlsbl_repo)

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 2

    def test_duplicate_commit_non_user_facing_rejected(self, rlsbl_repo):
        """Adding the same commit as non-user-facing twice is a hard error."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha,
            "description": "",
            "type": "",
            "user-facing": False,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)

        flags_second = {
            "commits": sha,
            "description": "",
            "type": "",
            "user-facing": False,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags_second, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_duplicate_commit_user_facing_vs_non_user_facing_warns(self, rlsbl_repo, capsys):
        """User-facing then non-user-facing for the same commit warns but succeeds."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha[:12],
            "description": "A feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)

        flags_second = {
            "commits": sha[:12],
            "description": "",
            "type": "",
            "user-facing": False,
            "auto-commit": False,
        }
        cmd_add(flags_second, project_root=rlsbl_repo)

        entries = read_unreleased(get_changes_dir("."))
        assert len(entries) == 2

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_duplicate_same_type_message_names_id_and_says_nothing_written(self, rlsbl_repo, capsys):
        """Hard-block message references the stable entry id, says nothing was
        written, and names the remediation."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha[:12],
            "description": "First feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)
        existing = read_unreleased(get_changes_dir("."))
        first_id = existing[0].id
        assert first_id  # sanity: added entries carry a stable id

        capsys.readouterr()  # clear buffered output from the first add

        flags_second = {
            "commits": sha[:12],
            "description": "Duplicate feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit):
            cmd_add(flags_second, project_root=rlsbl_repo)

        err = capsys.readouterr().err
        assert first_id in err, err
        assert "Nothing was written" in err, err
        assert "rlsbl changelog edit" in err, err
        # Reference is by stable id, not an unstable positional ordinal.
        assert f"entry {first_id}" in err, err
        assert "entry 1:" not in err, err

    def test_duplicate_different_type_message_says_written_and_id(self, rlsbl_repo, capsys):
        """Allowed-duplicate message references the stable id, states the entry
        WAS written, and cites max_entries_per_commit."""
        sha = _make_commit(rlsbl_repo)
        flags_first = {
            "commits": sha[:12],
            "description": "Feature entry",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_first, project_root=rlsbl_repo)
        existing = read_unreleased(get_changes_dir("."))
        first_id = existing[0].id
        assert first_id

        capsys.readouterr()  # clear buffered output from the first add

        flags_second = {
            "commits": sha[:12],
            "description": "Fix entry",
            "type": "fix",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags_second, project_root=rlsbl_repo)

        err = capsys.readouterr().err
        assert first_id in err, err
        assert "WAS" in err, err
        assert "max_entries_per_commit" in err, err
        # The allowed duplicate was actually written.
        assert len(read_unreleased(get_changes_dir("."))) == 2

    def test_invalid_type_rejected_writes_nothing(self, rlsbl_repo):
        """An out-of-enum --type aborts before writing any entry."""
        sha = _make_commit(rlsbl_repo)
        flags = {
            "commits": sha,
            "description": "Something",
            "type": "performance",
            "user-facing": True,
            "auto-commit": False,
        }
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            with pytest.raises(SystemExit) as exc_info:
                cmd_add(flags, project_root=rlsbl_repo)
        assert exc_info.value.code == 1
        # Nothing written and nothing committed.
        assert read_unreleased(get_changes_dir(".")) == []
        mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_generate tests
# ---------------------------------------------------------------------------


class TestCmdGenerate:
    """Tests for cmd_generate."""

    def test_produces_files(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Added feature X",
                type="feature",
            ),
        )

        cmd_generate({"dry-run": False}, project_root=rlsbl_repo)

        changelog_path = rlsbl_repo / "CHANGELOG.md"
        assert changelog_path.exists()
        content = changelog_path.read_text()
        assert "Added feature X" in content

    def test_dry_run(self, rlsbl_repo, capsys):
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Feature Y",
                type="feature",
            ),
        )

        cmd_generate({"dry-run": True}, project_root=rlsbl_repo)

        # Dry-run should NOT write CHANGELOG.md
        changelog_path = rlsbl_repo / "CHANGELOG.md"
        assert not changelog_path.exists()

        # But the preview should be printed
        captured = capsys.readouterr()
        assert "Feature Y" in captured.out
        assert "dry-run" in captured.out

    def test_auto_commits(self, rlsbl_repo):
        """After generating, commit_files is called with changed files."""
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Added feature Z",
                type="feature",
            ),
        )

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            with mock.patch(
                "rlsbl.commands.changelog_cmd._get_generated_files",
                return_value=["CHANGELOG.md"],
            ):
                cmd_generate({"dry-run": False}, project_root=rlsbl_repo)
                mock_commit.assert_called_once()
                call_args = mock_commit.call_args
                assert call_args[0][0] == "changelog: regenerate from JSONL"
                assert call_args[0][1] == ["CHANGELOG.md"]
                assert call_args[1]["allow_failure"] is True

    def test_auto_commit_has_autogenerated_trailer(self, rlsbl_repo):
        """The auto-commit from cmd_generate passes autogenerated=True."""
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Added feature Z",
                type="feature",
            ),
        )

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            with mock.patch(
                "rlsbl.commands.changelog_cmd._get_generated_files",
                return_value=["CHANGELOG.md"],
            ):
                cmd_generate({"dry-run": False}, project_root=rlsbl_repo)
                mock_commit.assert_called_once()
                assert mock_commit.call_args[1].get("autogenerated", True) is True

    def test_no_commit_flag(self, rlsbl_repo):
        """With --no-commit, commit_files is NOT called."""
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Added feature W",
                type="feature",
            ),
        )

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_generate({"dry-run": False, "auto-commit": False}, project_root=rlsbl_repo)
            mock_commit.assert_not_called()

    def test_dry_run_does_not_commit(self, rlsbl_repo, capsys):
        """Dry-run should NOT trigger auto-commit."""
        sha = _make_commit(rlsbl_repo)
        changes_dir = get_changes_dir(".")
        append_entry(
            changes_dir,
            ChangelogEntry(
                commits=[sha],
                user_facing=True,
                description="Feature Q",
                type="feature",
            ),
        )

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files") as mock_commit:
            cmd_generate({"dry-run": True}, project_root=rlsbl_repo)
            mock_commit.assert_not_called()

    def test_no_changes_dir(self, tmp_path, monkeypatch):
        """Error when .rlsbl/changes/ does not exist."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_generate({"dry-run": False}, project_root=tmp_path)
        assert exc_info.value.code == 1


class TestGeneratedFileDiscoverySurvivesGitQuoting:
    """The files a regeneration commits are named as they are on disk.

    Both discovery helpers read ``git status`` and hand what they find straight
    to ``commit_files``. Read in git's default porcelain form, any path outside
    plain ASCII arrives C-quoted (``"cor\\303\\251/x.md"``), and a commit naming
    that literal names a file no filesystem has: the regenerated changelog is
    then silently left uncommitted.
    """

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")
        return repo

    def test_filter_dirty_files_names_a_unicode_path_verbatim(self, tmp_path):
        """A releasable whose directory is not plain ASCII still commits."""
        from rlsbl.commands.changelog_cmd import _filter_dirty_files

        repo = self._repo(tmp_path)
        changes = repo / ".rlsbl-monorepo" / "releasables" / "coré ext" / "changes"
        changes.mkdir(parents=True)
        md = changes / "0.1.0.md"
        md.write_text("## 0.1.0\n")
        changelog = repo / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")

        found = _filter_dirty_files([str(md), str(changelog)], str(repo))

        assert sorted(found) == sorted([str(md), str(changelog)])
        assert all(os.path.exists(p) for p in found), found

    def test_generated_files_are_reported_as_they_are_on_disk(
        self, tmp_path, monkeypatch,
    ):
        """The per-version .md sweep reports real paths, not quoted spellings.

        The name here carries a space and a non-ASCII character because that is
        what git quotes; what is pinned is that the helper reports whatever git
        reports, unchanged, so the commit can find it.
        """
        from rlsbl.commands.changelog_cmd import _get_generated_files

        repo = self._repo(tmp_path)
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "0.1.0 é.md").write_text("## 0.1.0\n")
        (repo / "CHANGELOG.md").write_text("# Changelog\n")
        monkeypatch.chdir(repo)

        found = _get_generated_files(str(repo))

        assert sorted(found) == sorted([
            str(repo / "CHANGELOG.md"), str(changes / "0.1.0 é.md"),
        ])
        assert all(os.path.exists(p) for p in found), found
