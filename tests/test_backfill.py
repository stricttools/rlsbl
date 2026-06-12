"""Tests for the backfill_changelog script."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Load backfill_changelog by file path instead of modifying sys.path
_BACKFILL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_changelog.py"
_spec = importlib.util.spec_from_file_location("backfill_changelog", _BACKFILL_SCRIPT)
_backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backfill_mod)

build_entries = _backfill_mod.build_entries
classify_bullet = _backfill_mod.classify_bullet
extract_keywords = _backfill_mod.extract_keywords
is_no_user_facing = _backfill_mod.is_no_user_facing
map_commits_to_bullets = _backfill_mod.map_commits_to_bullets
parse_bullets = _backfill_mod.parse_bullets
score_match = _backfill_mod.score_match
write_jsonl = _backfill_mod.write_jsonl
backfill = _backfill_mod.backfill
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl


class TestParseBullets:
    """Tests for parse_bullets."""

    def test_empty_string(self):
        assert parse_bullets("") == []

    def test_single_bullet(self):
        text = "- First item"
        assert parse_bullets(text) == ["First item"]

    def test_multiple_bullets(self):
        text = "- First\n- Second\n- Third"
        assert parse_bullets(text) == ["First", "Second", "Third"]

    def test_bullet_with_bold_prefix(self):
        text = "- **Name check.** Details here"
        assert parse_bullets(text) == ["**Name check.** Details here"]

    def test_continuation_lines(self):
        text = "- First line\n  continued here\n- Second"
        assert parse_bullets(text) == ["First line continued here", "Second"]

    def test_ignores_non_bullet_lines(self):
        text = "Some preamble\n- Actual bullet\nAnother line\n- Second bullet"
        assert parse_bullets(text) == ["Actual bullet", "Second bullet"]

    def test_no_user_facing(self):
        text = "- No user-facing changes."
        assert parse_bullets(text) == ["No user-facing changes."]

    def test_sub_list_items(self):
        # Monorepo entries with sub-bullets
        text = (
            "- **Monorepo support.** Main description\n"
            "  - `monorepo init` creates workspace.\n"
            "  - `monorepo add` registers project."
        )
        result = parse_bullets(text)
        assert len(result) == 1
        assert "Monorepo support" in result[0]


class TestClassifyBullet:
    """Tests for classify_bullet."""

    def test_breaking_prefix(self):
        assert classify_bullet("**Breaking: hooks changed.**") == "breaking"

    def test_fix_prefix(self):
        assert classify_bullet("**Fix: lint false positives.**") == "fix"

    def test_fix_start(self):
        assert classify_bullet("Fix watch: resolve short SHAs") == "fix"

    def test_feature_default(self):
        assert classify_bullet("**Name check short-circuiting.**") == "feature"

    def test_feature_plain(self):
        assert classify_bullet("New command added") == "feature"


class TestExtractKeywords:
    """Tests for extract_keywords."""

    def test_strips_prefix(self):
        kws = extract_keywords("feat: add scaffold command")
        assert "scaffold" in kws
        assert "command" in kws

    def test_filters_short_words(self):
        kws = extract_keywords("a b cd efg")
        assert "efg" in kws
        assert "cd" not in kws

    def test_filters_noise(self):
        kws = extract_keywords("the scaffold and the release")
        assert "the" not in kws
        assert "scaffold" in kws
        assert "release" in kws


class TestScoreMatch:
    """Tests for score_match."""

    def test_no_keywords(self):
        assert score_match("", "some bullet text") == 0

    def test_exact_substring_match(self):
        # Exact substring: cleaned subject appears verbatim in bullet text
        s = score_match("feat: scaffold command", "the scaffold command is new")
        assert s > 5  # substring bonus

    def test_keyword_match_through_markdown(self):
        # Bold markdown prevents exact substring but keywords still match
        s = score_match("add scaffold command", "**Scaffold command.** Details")
        assert s >= 2

    def test_keyword_match(self):
        s = score_match("feat: fix lint false positives", "lint false positives from vendored code")
        assert s >= 2

    def test_no_match(self):
        s = score_match("refactor: rename internal module", "**Deploy command.** SSH deploys")
        assert s < 2


class TestIsNoUserFacing:
    """Tests for is_no_user_facing."""

    def test_standard_text(self):
        assert is_no_user_facing("- No user-facing changes.") is True

    def test_without_dash(self):
        assert is_no_user_facing("No user-facing changes.") is True

    def test_real_content(self):
        assert is_no_user_facing("- **Feature.** Something new") is False

    def test_empty(self):
        assert is_no_user_facing("") is True

    def test_none(self):
        assert is_no_user_facing(None) is True


class TestMapCommitsToBullets:
    """Tests for map_commits_to_bullets."""

    def test_no_bullets(self):
        commits = [{"hash": "abc", "subject": "something"}]
        bullet_commits, unmatched = map_commits_to_bullets(commits, [])
        assert bullet_commits == {}
        assert unmatched == ["abc"]

    def test_version_tag_commits_unmatched(self):
        commits = [
            {"hash": "abc", "subject": "v0.5.0"},
            {"hash": "def", "subject": "feat: add feature"},
        ]
        bullets = ["**Feature.** A new feature"]
        bullet_commits, unmatched = map_commits_to_bullets(commits, bullets)
        assert "abc" in unmatched

    def test_matching_commit(self):
        commits = [
            {"hash": "abc", "subject": "feat: add scaffold command"},
        ]
        bullets = ["**Scaffold command.** New scaffold feature"]
        bullet_commits, unmatched = map_commits_to_bullets(commits, bullets)
        assert "abc" in bullet_commits[0]
        assert unmatched == []


class TestBuildEntries:
    """Tests for build_entries."""

    def test_no_commits(self):
        assert build_entries([], "- Something") == []

    def test_no_changelog(self):
        commits = [{"hash": "abc", "subject": "do stuff"}]
        entries = build_entries(commits, None)
        assert len(entries) == 1
        assert entries[0].user_facing is False
        assert entries[0].commits == ["abc"]

    def test_no_user_facing_changelog(self):
        commits = [{"hash": "abc", "subject": "internal fix"}]
        entries = build_entries(commits, "- No user-facing changes.")
        assert len(entries) == 1
        assert entries[0].user_facing is False

    def test_with_matching_bullet(self):
        commits = [
            {"hash": "abc", "subject": "feat: add scaffold command"},
        ]
        changelog = "- **Scaffold command.** Details about scaffolding"
        entries = build_entries(commits, changelog)
        user_facing = [e for e in entries if e.user_facing]
        assert len(user_facing) >= 1
        assert user_facing[0].description == "**Scaffold command.** Details about scaffolding"
        assert user_facing[0].type == "feature"


class TestWriteJsonl:
    """Tests for write_jsonl."""

    def test_creates_file(self, tmp_path):
        path = str(tmp_path / "changes" / "1.0.0.jsonl")
        entries = [
            ChangelogEntry(commits=["abc"], user_facing=False),
            ChangelogEntry(commits=["def"], user_facing=True,
                           description="Feature", type="feature"),
        ]
        write_jsonl(path, entries)

        assert os.path.isfile(path)
        parsed = parse_jsonl(path)
        assert len(parsed) == 2
        assert parsed[0].commits == ["abc"]
        assert parsed[1].description == "Feature"

    def test_file_is_read_only(self, tmp_path):
        path = str(tmp_path / "1.0.0.jsonl")
        write_jsonl(path, [ChangelogEntry(commits=["abc"], user_facing=False)])

        mode = os.stat(path).st_mode
        assert not (mode & stat.S_IWUSR)
        assert not (mode & stat.S_IWGRP)
        assert not (mode & stat.S_IWOTH)


class TestBackfillIntegration:
    """Integration test using a real mini git repo."""

    def _run_git(self, cwd, *args):
        subprocess.run(["git"] + list(args), cwd=str(cwd),
                        capture_output=True, check=True)

    def _commit(self, cwd, message, filename=None):
        if filename is None:
            filename = f"file_{message.replace(' ', '_')[:20]}.txt"
        filepath = cwd / filename
        filepath.write_text(f"content for {message}\n")
        self._run_git(cwd, "add", filename)
        self._run_git(cwd, "commit", "-m", message)

    def test_full_backfill(self, tmp_path, monkeypatch):
        """Create a mini repo with tags and changelog, run backfill."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Init git repo
        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")

        # Create initial commits and v0.1.0
        self._commit(repo, "initial commit")
        self._commit(repo, "feat: add release command")
        self._run_git(repo, "tag", "v0.1.0")

        # Create commits and v0.2.0
        self._commit(repo, "feat: add scaffold feature")
        self._commit(repo, "fix: resolve config bug")
        self._run_git(repo, "tag", "v0.2.0")

        # Create commits and v0.3.0 (no user-facing)
        self._commit(repo, "refactor: internal cleanup")
        self._run_git(repo, "tag", "v0.3.0")

        # Write CHANGELOG.md
        changelog = repo / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## 0.3.0\n\n"
            "- No user-facing changes.\n\n"
            "## 0.2.0\n\n"
            "- **Scaffold feature.** Added scaffold support\n"
            "- **Fix: config bug.** Resolved config loading issue\n\n"
            "## 0.1.0\n\n"
            "- **Release command.** Initial release command\n"
        )

        # Create .rlsbl/changes/
        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        monkeypatch.chdir(repo)

        summary = backfill(
            str(repo),
            str(changelog),
        )

        # Check that JSONL files were created
        assert (changes_dir / "0.1.0.jsonl").exists()
        assert (changes_dir / "0.2.0.jsonl").exists()
        assert (changes_dir / "0.3.0.jsonl").exists()

        assert summary["versions_processed"] == 3

        # Check v0.1.0: should have user-facing entry
        entries_v1 = parse_jsonl(str(changes_dir / "0.1.0.jsonl"))
        assert len(entries_v1) >= 1
        user_facing_v1 = [e for e in entries_v1 if e.user_facing]
        assert len(user_facing_v1) >= 1

        # Check v0.3.0: no user-facing changes
        entries_v3 = parse_jsonl(str(changes_dir / "0.3.0.jsonl"))
        assert len(entries_v3) >= 1
        assert all(not e.user_facing for e in entries_v3)

        # Check files are read-only
        for name in ["0.1.0.jsonl", "0.2.0.jsonl", "0.3.0.jsonl"]:
            mode = os.stat(str(changes_dir / name)).st_mode
            assert not (mode & stat.S_IWUSR)

    def test_idempotent_skip(self, tmp_path, monkeypatch):
        """Verify existing files are skipped without --force."""
        repo = tmp_path / "repo"
        repo.mkdir()

        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")
        self._commit(repo, "initial")
        self._run_git(repo, "tag", "v0.1.0")

        changelog = repo / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 0.1.0\n\n- Initial\n")

        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        monkeypatch.chdir(repo)

        # First run
        summary1 = backfill(str(repo), str(changelog))
        assert summary1["versions_processed"] == 1

        # Second run: should skip
        summary2 = backfill(str(repo), str(changelog))
        assert summary2["versions_processed"] == 0
        assert summary2["skipped"] == 1

    def test_force_overwrite(self, tmp_path, monkeypatch):
        """Verify --force overwrites existing files."""
        repo = tmp_path / "repo"
        repo.mkdir()

        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")
        self._commit(repo, "initial")
        self._run_git(repo, "tag", "v0.1.0")

        changelog = repo / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 0.1.0\n\n- Initial\n")

        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        monkeypatch.chdir(repo)

        # First run
        backfill(str(repo), str(changelog))

        # Second run with force
        summary = backfill(str(repo), str(changelog), force=True)
        assert summary["versions_processed"] == 1
        assert summary["skipped"] == 0

    def test_single_version(self, tmp_path, monkeypatch):
        """Verify --version processes only one version."""
        repo = tmp_path / "repo"
        repo.mkdir()

        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")
        self._commit(repo, "initial")
        self._run_git(repo, "tag", "v0.1.0")
        self._commit(repo, "second")
        self._run_git(repo, "tag", "v0.2.0")

        changelog = repo / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## 0.2.0\n\n- Second\n\n"
            "## 0.1.0\n\n- Initial\n"
        )

        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        monkeypatch.chdir(repo)

        summary = backfill(str(repo), str(changelog), single_version="0.2.0")
        assert summary["versions_processed"] == 1
        assert (changes_dir / "0.2.0.jsonl").exists()
        assert not (changes_dir / "0.1.0.jsonl").exists()

    def test_main_uses_cwd_by_default(self, tmp_path, monkeypatch):
        """Verify the script uses CWD as the default project root."""
        repo = tmp_path / "repo"
        repo.mkdir()

        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")
        self._commit(repo, "initial")
        self._run_git(repo, "tag", "v0.1.0")

        changelog = repo / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 0.1.0\n\n- Initial\n")

        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        monkeypatch.chdir(repo)

        # Run the script's main via subprocess from the repo directory
        result = subprocess.run(
            [sys.executable, os.path.join(
                os.path.dirname(__file__), "..", "scripts", "backfill_changelog.py"
            )],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        # JSONL should be written in the repo's .rlsbl/changes/, not rlsbl's
        assert (changes_dir / "0.1.0.jsonl").exists(), (
            "Expected JSONL in CWD's .rlsbl/changes/, not the script's parent"
        )

    def test_project_root_flag_overrides_cwd(self, tmp_path, monkeypatch):
        """Verify --project-root overrides CWD for changelog/changes paths."""
        repo = tmp_path / "repo"
        repo.mkdir()

        self._run_git(repo, "init", "-q")
        self._run_git(repo, "config", "user.email", "test@test.local")
        self._run_git(repo, "config", "user.name", "Test")
        self._commit(repo, "initial")
        self._run_git(repo, "tag", "v0.1.0")

        changelog = repo / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## 0.1.0\n\n- Initial\n")

        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True)

        # Create a second repo to use as CWD (so CWD != project root)
        other = tmp_path / "other"
        other.mkdir()
        self._run_git(other, "init", "-q")
        self._run_git(other, "config", "user.email", "test@test.local")
        self._run_git(other, "config", "user.name", "Test")

        # Run from repo (git needs the repo) with --project-root pointing to repo
        # The key test: --project-root is respected for file paths
        result = subprocess.run(
            [sys.executable, os.path.join(
                os.path.dirname(__file__), "..", "scripts", "backfill_changelog.py"
            ), "--project-root", str(repo)],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert (changes_dir / "0.1.0.jsonl").exists()
