"""Integration tests for the prepush check system.

Tests the four prepush checks (prepush-changelog-coverage,
prepush-gitignore-guard, prepush-manual-warning, test-suite) as
registered on the strictcli check system, including depends_on ordering.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import strictcli

from conftest import git_head, make_commit, make_ctx, run_git
from rlsbl import app
from rlsbl.context import ProjectContext


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def prepush_repo(tmp_path, monkeypatch):
    """Create a git repo with .rlsbl/changes/ set up for prepush testing.

    Returns the repo Path. The repo has an initial commit, a v0.0.0 tag,
    and an empty unreleased.jsonl.
    """
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

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")

    (repo / ".rlsbl" / "config.json").write_text(json.dumps({"private": False}))

    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

    return repo


def _make_push_stdin(repo, local_sha, remote_sha):
    """Build a push stdin string in the format git passes to pre-push hooks."""
    return f"refs/heads/main {local_sha} refs/heads/main {remote_sha}"


# ------------------------------------------------------------------
# Test 1: changelog coverage blocks push
# ------------------------------------------------------------------


class TestChangelogCoverageBlocksPush:
    """prepush-changelog-coverage returns fail for uncovered commits."""

    def test_uncovered_commit_fails(self, prepush_repo):
        base_sha = git_head(prepush_repo)

        # Make a code commit with no JSONL entry
        (prepush_repo / "src.py").write_text("x = 1\n")
        run_git(prepush_repo, "add", "src.py")
        run_git(prepush_repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(prepush_repo)

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = _make_push_stdin(prepush_repo, head_sha, base_sha)

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail"


# ------------------------------------------------------------------
# Test 2: changelog coverage passes when covered
# ------------------------------------------------------------------


class TestChangelogCoveragePassesWhenCovered:
    """prepush-changelog-coverage returns pass when all commits are covered."""

    def test_covered_commit_passes(self, prepush_repo):
        base_sha = git_head(prepush_repo)

        # Make a code commit
        (prepush_repo / "src.py").write_text("x = 1\n")
        run_git(prepush_repo, "add", "src.py")
        run_git(prepush_repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(prepush_repo)

        # Add a JSONL entry covering the commit
        changes = prepush_repo / ".rlsbl" / "changes"
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = _make_push_stdin(prepush_repo, head_sha, base_sha)

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"


# ------------------------------------------------------------------
# Test 3: changelog coverage skips without push context
# ------------------------------------------------------------------


class TestChangelogCoverageSkipsWithoutPushContext:
    """prepush-changelog-coverage returns skip when push_stdin is not set."""

    def test_no_push_context_skips(self, prepush_repo):
        # Make a commit so the repo isn't trivially empty
        make_commit(prepush_repo, "src.py", "feat: something")

        ctx = make_ctx(prepush_repo)
        # push_stdin defaults to None -- do NOT set it
        assert ctx.push_stdin is None

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "skip"


# ------------------------------------------------------------------
# Test 4: gitignore guard fails when gitignored
# ------------------------------------------------------------------


class TestGitignoreGuardFailsWhenGitignored:
    """prepush-gitignore-guard returns fail when unreleased.jsonl is gitignored."""

    def test_gitignored_jsonl_fails(self, mock_git_repo):
        # Add gitignore BEFORE tracking .rlsbl/ files, so git check-ignore
        # sees them as ignored (already-tracked files bypass .gitignore).
        (mock_git_repo / ".gitignore").write_text(
            ".rlsbl/changes/unreleased.jsonl\n"
        )
        run_git(mock_git_repo, "add", ".gitignore")
        run_git(mock_git_repo, "commit", "-q", "-m", "add gitignore")

        ctx = make_ctx(mock_git_repo)
        result = app._check_defs["prepush-gitignore-guard"].impl(ctx)
        assert result.status == "fail"
        assert "gitignored" in result.message.lower()


# ------------------------------------------------------------------
# Test 5: gitignore guard passes normally
# ------------------------------------------------------------------


class TestGitignoreGuardPassesNormally:
    """prepush-gitignore-guard returns pass when nothing is gitignored."""

    def test_no_gitignore_passes(self, mock_git_repo):
        ctx = make_ctx(mock_git_repo)
        result = app._check_defs["prepush-gitignore-guard"].impl(ctx)
        assert result.status == "pass"


# ------------------------------------------------------------------
# Test 6: manual warning on release branch
# ------------------------------------------------------------------


class TestManualWarningOnReleaseBranch:
    """prepush-manual-warning returns warn on manual push to release branch."""

    def test_manual_push_to_main_warns(self, prepush_repo, monkeypatch):
        head_sha = git_head(prepush_repo)
        zero_sha = "0" * 40

        # Ensure RLSBL_RELEASE_PUSH is NOT set
        monkeypatch.delenv("RLSBL_RELEASE_PUSH", raising=False)

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = _make_push_stdin(prepush_repo, head_sha, zero_sha)

        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "warn"
        assert "main" in result.message


# ------------------------------------------------------------------
# Test 7: manual warning suppressed during release
# ------------------------------------------------------------------


class TestManualWarningSuppressedDuringRelease:
    """prepush-manual-warning returns pass when RLSBL_RELEASE_PUSH=1."""

    def test_release_push_suppresses_warning(self, prepush_repo, monkeypatch):
        head_sha = git_head(prepush_repo)
        zero_sha = "0" * 40

        monkeypatch.setenv("RLSBL_RELEASE_PUSH", "1")

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = _make_push_stdin(prepush_repo, head_sha, zero_sha)

        result = app._check_defs["prepush-manual-warning"].impl(ctx)
        assert result.status == "pass"


# ------------------------------------------------------------------
# Test 8: test-suite runs and passes
# ------------------------------------------------------------------


class TestTestSuiteRunsAndPasses:
    """test-suite returns pass when project tests pass."""

    def test_pypi_tests_pass(self, prepush_repo):
        # Create a pyproject.toml so pypi target is detected
        (prepush_repo / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )

        ctx = make_ctx(prepush_repo)

        with patch("rlsbl.testing.run_project_tests", return_value=True) as mock_tests:
            result = app._check_defs["test-suite"].impl(ctx)

        assert result.status == "pass"
        mock_tests.assert_called_once_with(
            "pypi", project_dir=str(prepush_repo), config=ctx.config,
        )


# ------------------------------------------------------------------
# Test 9: test-suite fails on test failure
# ------------------------------------------------------------------


class TestTestSuiteFailsOnTestFailure:
    """test-suite returns fail when project tests fail."""

    def test_pypi_tests_fail(self, prepush_repo):
        (prepush_repo / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )

        ctx = make_ctx(prepush_repo)

        with patch("rlsbl.testing.run_project_tests", return_value=False):
            result = app._check_defs["test-suite"].impl(ctx)

        assert result.status == "fail"


# ------------------------------------------------------------------
# Test 10: depends_on ordering
# ------------------------------------------------------------------


class TestDependsOnOrdering:
    """test-suite is skipped when its dependency prepush-changelog-coverage fails."""

    def test_test_suite_skipped_when_coverage_fails(self, prepush_repo):
        base_sha = git_head(prepush_repo)

        # Create pyproject.toml so test-suite has a target to run
        (prepush_repo / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )

        # Make a code commit with no JSONL coverage
        (prepush_repo / "src.py").write_text("x = 1\n")
        run_git(prepush_repo, "add", "src.py")
        run_git(prepush_repo, "commit", "-q", "-m", "feat: uncovered")
        head_sha = git_head(prepush_repo)

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = _make_push_stdin(prepush_repo, head_sha, base_sha)

        # Run all prepush checks via the check system
        selected = strictcli._filter_checks(
            app._check_defs, "prepush", None, False,
        )
        order = strictcli._resolve_check_order(app._check_defs, selected)
        results, exit_code = strictcli._run_checks(app, order, ctx, True)

        results_dict = {name: result for name, result in results}

        # changelog-coverage must have failed
        assert results_dict["prepush-changelog-coverage"].status == "fail"

        # test-suite must be skipped because its dependency failed
        assert results_dict["test-suite"].status == "skip"
        assert "prepush-changelog-coverage" in results_dict["test-suite"].message

        assert exit_code == 1
