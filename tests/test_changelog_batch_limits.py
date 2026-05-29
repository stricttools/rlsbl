"""Tests for the batch_limits validation checks (Phase 4).

Covers:
- _get_batch_limits_config: defaults + override + type validation
- check_batch_size_commits: limit enforcement + per-entry exclusions
- check_batch_size_entries: cross-version limit + per-commit exclusions
- validate_unreleased: integrates both new checks (7 total)
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from conftest import run_git as _run_git, git_head as _git_head, make_commit as _make_commit
from rlsbl.changelog.files import append_entry
from rlsbl.changelog.schema import ChangelogEntry
from rlsbl.changelog.validate import (
    _get_batch_limits_config,
    check_batch_size_commits,
    check_batch_size_entries,
    validate_unreleased,
)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Create a git repo with an initial commit and a baseline version tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    _run_git(repo, "tag", "v0.0.0")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)

    return repo


def _write_config(repo, batch_limits):
    """Write `.rlsbl/config.json` with the given batch_limits payload."""
    cfg = repo / ".rlsbl" / "config.json"
    cfg.write_text(json.dumps({"batch_limits": batch_limits}))


# ---------------------------------------------------------------------------
# _get_batch_limits_config
# ---------------------------------------------------------------------------

class TestGetBatchLimitsConfig:
    def test_defaults_when_no_config(self, git_repo):
        cfg = _get_batch_limits_config(git_repo)
        assert cfg["max_commits_per_entry"] == 5
        assert cfg["max_entries_per_commit"] == 5
        assert cfg["exclusions"] == []

    def test_defaults_when_partial_config(self, git_repo):
        _write_config(git_repo, {"max_commits_per_entry": 7})
        cfg = _get_batch_limits_config(git_repo)
        assert cfg["max_commits_per_entry"] == 7
        assert cfg["max_entries_per_commit"] == 5
        assert cfg["exclusions"] == []

    def test_full_override(self, git_repo):
        _write_config(
            git_repo,
            {
                "max_commits_per_entry": 10,
                "max_entries_per_commit": 4,
                "exclusions": [{"reason": "x", "commits": ["abc"]}],
            },
        )
        cfg = _get_batch_limits_config(git_repo)
        assert cfg["max_commits_per_entry"] == 10
        assert cfg["max_entries_per_commit"] == 4
        assert cfg["exclusions"] == [{"reason": "x", "commits": ["abc"]}]

    def test_wrong_type_uses_default(self, git_repo, capsys):
        _write_config(
            git_repo,
            {
                "max_commits_per_entry": "not-an-int",
                "exclusions": "not-a-list",
            },
        )
        cfg = _get_batch_limits_config(git_repo)
        assert cfg["max_commits_per_entry"] == 5
        assert cfg["exclusions"] == []
        err = capsys.readouterr().err
        assert "max_commits_per_entry" in err
        assert "exclusions" in err


# ---------------------------------------------------------------------------
# check_batch_size_commits
# ---------------------------------------------------------------------------

class TestCheckBatchSizeCommits:
    def test_default_max_5_passes_with_5(self, git_repo):
        entries = [ChangelogEntry(commits=["a" * 7] * 5, user_facing=False)]
        cfg = {"max_commits_per_entry": 5, "exclusions": []}
        passed, details = check_batch_size_commits(entries, cfg)
        assert passed is True
        assert details == []

    def test_default_max_5_fails_with_6(self, git_repo):
        entries = [ChangelogEntry(commits=[f"{c}" * 7 for c in "abcdef"], user_facing=False)]
        cfg = {"max_commits_per_entry": 5, "exclusions": []}
        passed, details = check_batch_size_commits(entries, cfg)
        assert passed is False
        assert len(details) == 1
        assert "6 commits" in details[0]
        assert "max: 5" in details[0]

    def test_override_max_3_passes(self, git_repo):
        entries = [ChangelogEntry(commits=["a" * 7, "b" * 7, "c" * 7], user_facing=False)]
        cfg = {"max_commits_per_entry": 3, "exclusions": []}
        passed, details = check_batch_size_commits(entries, cfg)
        assert passed is True

    def test_override_max_3_fails_with_4(self, git_repo):
        entries = [ChangelogEntry(commits=["a" * 7, "b" * 7, "c" * 7, "d" * 7], user_facing=False)]
        cfg = {"max_commits_per_entry": 3, "exclusions": []}
        passed, details = check_batch_size_commits(entries, cfg)
        assert passed is False
        assert "4 commits" in details[0]
        assert "max: 3" in details[0]

    def test_exclusion_silences_entry_violation(self, git_repo):
        # 18 entries of dummies, where line 18 is the violator (10 commits).
        entries = [ChangelogEntry(commits=["x" * 7], user_facing=False)] * 17
        entries = list(entries) + [
            ChangelogEntry(commits=[f"{i:07d}" for i in range(10)], user_facing=False),
        ]
        cfg = {
            "max_commits_per_entry": 5,
            "exclusions": [{"reason": "early-project batch entry", "entries": [{"version": "0.5.0", "line": 18}]}],
        }
        passed, details = check_batch_size_commits(entries, cfg, version="0.5.0")
        assert passed is True
        assert details == []

    def test_exclusion_does_not_silence_other_versions(self, git_repo):
        """Exclusion for version 0.5.0 line 18 must not silence unreleased line 18."""
        entries = [ChangelogEntry(commits=["x" * 7], user_facing=False)] * 17 + [
            ChangelogEntry(commits=[f"{i:07d}" for i in range(10)], user_facing=False),
        ]
        cfg = {
            "max_commits_per_entry": 5,
            "exclusions": [{"reason": "irrelevant", "entries": [{"version": "0.5.0", "line": 18}]}],
        }
        passed, details = check_batch_size_commits(entries, cfg, version="unreleased")
        assert passed is False
        assert "unreleased.jsonl line 18" in details[0]

    def test_empty_entries_passes(self, git_repo):
        passed, details = check_batch_size_commits([], {"max_commits_per_entry": 5, "exclusions": []})
        assert passed is True
        assert details == []


# ---------------------------------------------------------------------------
# check_batch_size_entries
# ---------------------------------------------------------------------------

class TestCheckBatchSizeEntries:
    def test_commit_in_five_entries_passes_default(self, git_repo):
        commit = "a" * 40
        entries_by_version = {
            "unreleased": [
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
            ],
        }
        cfg = {"max_entries_per_commit": 5, "exclusions": []}
        passed, details = check_batch_size_entries(entries_by_version, cfg)
        assert passed is True

    def test_commit_in_six_entries_fails_default(self, git_repo):
        commit = "b" * 40
        entries_by_version = {
            "unreleased": [
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
            ],
        }
        cfg = {"max_entries_per_commit": 5, "exclusions": []}
        passed, details = check_batch_size_entries(entries_by_version, cfg)
        assert passed is False
        assert "6 entries" in details[0]
        assert "max: 5" in details[0]

    def test_exclusion_silences_commit_violation(self, git_repo):
        commit = "c" * 40
        entries_by_version = {
            "unreleased": [ChangelogEntry(commits=[commit], user_facing=False)] * 8,
        }
        cfg = {
            "max_entries_per_commit": 5,
            "exclusions": [{"reason": "retroactive", "commits": [commit]}],
        }
        passed, details = check_batch_size_entries(entries_by_version, cfg)
        assert passed is True
        assert details == []

    def test_cross_version_check_passes_at_max(self, git_repo):
        """Commit appearing 5 times across versions (4 in 0.32.0 + 1 unreleased), max 5 => pass."""
        commit = "d" * 40
        filler = [
            ChangelogEntry(commits=[f"{i:040d}"], user_facing=False) for i in range(17)
        ]
        entries_by_version = {
            "unreleased": [ChangelogEntry(commits=[commit], user_facing=False)],
            "0.32.0": filler + [ChangelogEntry(commits=[commit], user_facing=False)] * 4,
        }
        cfg = {"max_entries_per_commit": 5, "exclusions": []}
        passed, details = check_batch_size_entries(entries_by_version, cfg)
        assert passed is True

    def test_cross_version_check_fails_above_max(self, git_repo):
        """6 appearances across 2 versions, max 5 => fail; locations span versions."""
        commit = "e" * 40
        entries_by_version = {
            "unreleased": [
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
                ChangelogEntry(commits=[commit], user_facing=False),
            ],
            "0.32.0": [ChangelogEntry(commits=[commit], user_facing=False)],
        }
        cfg = {"max_entries_per_commit": 5, "exclusions": []}
        passed, details = check_batch_size_entries(entries_by_version, cfg)
        assert passed is False
        # message lists both unreleased and 0.32.0 locations
        assert "unreleased.jsonl:1" in details[0]
        assert "unreleased.jsonl:2" in details[0]
        assert "0.32.0.jsonl:1" in details[0]

    def test_empty_entries_passes(self, git_repo):
        passed, details = check_batch_size_entries({}, {"max_entries_per_commit": 5, "exclusions": []})
        assert passed is True
        assert details == []


# ---------------------------------------------------------------------------
# validate_unreleased integration
# ---------------------------------------------------------------------------

class TestValidateUnreleasedIntegration:
    def test_eight_checks_pass(self, git_repo):
        changes = str(git_repo / ".rlsbl" / "changes")
        sha = _make_commit(git_repo)
        append_entry(changes, ChangelogEntry(
            commits=[sha], user_facing=True, description="New feature", type="feature",
        ))

        result = validate_unreleased(changes, project_root=git_repo)
        assert result["passed"] is True
        for key in (
            "hashes_resolve",
            "in_range",
            "coverage",
            "no_orphans",
            "schema",
            "batch_size_commits",
            "batch_size_entries",
            "user_facing",
        ):
            assert key in result["checks"], f"missing check {key}"
            passed, _ = result["checks"][key]
            assert passed is True, f"check {key} failed unexpectedly"

    def test_batch_size_commits_fails(self, git_repo):
        """One unreleased entry with 6 commits exceeds the default max=5."""
        changes = str(git_repo / ".rlsbl" / "changes")
        shas = [_make_commit(git_repo, f"f{i}.txt") for i in range(6)]
        append_entry(changes, ChangelogEntry(commits=shas, user_facing=False))

        result = validate_unreleased(changes, project_root=git_repo)
        assert result["passed"] is False
        passed, details = result["checks"]["batch_size_commits"]
        assert passed is False
        assert "6 commits" in details[0]

    def test_batch_size_entries_fails(self, git_repo):
        """A single commit referenced by 6 unreleased entries exceeds default max=5."""
        changes = str(git_repo / ".rlsbl" / "changes")
        sha = _make_commit(git_repo)
        for _ in range(6):
            append_entry(changes, ChangelogEntry(commits=[sha], user_facing=False))

        result = validate_unreleased(changes, project_root=git_repo)
        assert result["passed"] is False
        passed, details = result["checks"]["batch_size_entries"]
        assert passed is False
        assert "6 entries" in details[0]

    def test_cross_version_violation_detected(self, git_repo):
        """Commit appearing in both unreleased.jsonl and a frozen 0.32.0.jsonl
        triggers batch_size_entries when total > max."""
        changes = git_repo / ".rlsbl" / "changes"
        sha = _make_commit(git_repo)

        # Pretend a past version included this commit five times already.
        versioned = changes / "0.32.0.jsonl"
        versioned.write_text(
            "\n".join(
                [json.dumps({"commits": [sha], "user_facing": False})] * 5
            )
            + "\n"
        )
        os.chmod(versioned, 0o444)

        # And unreleased references it once more (total 6 > max 5).
        append_entry(str(changes), ChangelogEntry(commits=[sha], user_facing=False))

        result = validate_unreleased(str(changes), project_root=git_repo)
        passed, details = result["checks"]["batch_size_entries"]
        assert passed is False
        assert "6 entries" in details[0]
        assert "0.32.0.jsonl" in details[0]
        assert "unreleased.jsonl" in details[0]

    def test_exclusion_silences_cross_version_violation(self, git_repo):
        """Adding the commit to batch_limits.exclusions.commits silences the violation."""
        changes = git_repo / ".rlsbl" / "changes"
        sha = _make_commit(git_repo)

        # Five appearances in the frozen version + one in unreleased = 6 > max 5.
        versioned = changes / "0.32.0.jsonl"
        versioned.write_text(
            "\n".join(
                [json.dumps({"commits": [sha], "user_facing": False})] * 5
            )
            + "\n"
        )
        os.chmod(versioned, 0o444)
        append_entry(str(changes), ChangelogEntry(commits=[sha], user_facing=False))

        # Write a config that exempts this commit.
        _write_config(git_repo, {"exclusions": [{"reason": "test", "commits": [sha]}]})

        result = validate_unreleased(str(changes), project_root=git_repo)
        passed, _ = result["checks"]["batch_size_entries"]
        assert passed is True
