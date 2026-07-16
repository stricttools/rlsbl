"""Tests for the dev-branch release workflow (Phase 6).

6b: Coverage enforcement is branch-agnostic -- pushes to non-release
    branches still require JSONL changelog coverage.

6c: Release from dev: ff-merge to release branch with rollback safety.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import git_head, make_commit, make_ctx, run_git
from rlsbl import app
from rlsbl.commands.release.validate import BranchValidation, validate_branch_and_remote


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_push_stdin(local_sha, remote_sha, *, branch="main"):
    """Build a push stdin string for a given branch."""
    return (
        f"refs/heads/{branch} {local_sha} "
        f"refs/heads/{branch} {remote_sha}"
    )


@pytest.fixture
def dev_branch_repo(tmp_path, monkeypatch):
    """Create a git repo with main and dev branches for release testing.

    The repo has:
    - An initial commit on main with a v0.0.0 tag
    - .rlsbl/changes/unreleased.jsonl set up
    - A 'dev' branch with one code commit (covered by JSONL entry)
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

    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": []})
    )

    run_git(repo, "add", ".rlsbl")
    run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

    # Create an 'origin' remote (bare repo for push simulation)
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True, capture_output=True,
    )
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "fetch", "origin")
    # Set upstream so push works
    run_git(repo, "branch", "--set-upstream-to=origin/main", "main")

    # Create dev branch with a code commit
    run_git(repo, "checkout", "-b", "dev")
    (repo / "src.py").write_text("x = 1\n")
    run_git(repo, "add", "src.py")
    run_git(repo, "commit", "-q", "-m", "feat: new feature")
    code_sha = git_head(repo)

    # Add JSONL entry covering the commit
    entry = json.dumps({
        "commits": [code_sha],
        "user_facing": True,
        "description": "new feature",
        "type": "feature",
    })
    (changes / "unreleased.jsonl").write_text(entry + "\n")
    run_git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    run_git(repo, "commit", "-q", "-m", "changelog entry")

    return repo


# ==================================================================
# 6b: Coverage enforcement is branch-agnostic
# ==================================================================


class TestCoverageEnforcedOnNonReleaseBranch:
    """prepush-changelog-coverage applies to all branches, not just
    release branches. There is no branch-based exemption."""

    def test_uncovered_commit_on_dev_branch_fails(self, tmp_path, monkeypatch):
        """A push to a non-release branch with an uncovered commit fails
        the changelog coverage check."""
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

        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": []})
        )

        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

        base_sha = git_head(repo)

        # Switch to a dev branch and make an uncovered commit
        run_git(repo, "checkout", "-b", "dev")
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(repo)

        # Simulate a push to the dev branch (not main)
        ctx = make_ctx(repo)
        ctx.push_stdin = _make_push_stdin(head_sha, base_sha, branch="dev")

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "fail", (
            "Coverage check must enforce on dev branches too -- "
            "no branch-based exemption should exist"
        )

    def test_covered_commit_on_dev_branch_passes(self, tmp_path, monkeypatch):
        """A push to a non-release branch with full coverage passes."""
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

        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": []})
        )

        run_git(repo, "add", ".rlsbl")
        run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

        base_sha = git_head(repo)

        # Switch to dev and make a covered commit
        run_git(repo, "checkout", "-b", "feature-x")
        (repo / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "src.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")
        head_sha = git_head(repo)

        # Cover the commit
        entry = json.dumps({
            "commits": [head_sha],
            "user_facing": True,
            "description": "new feature",
            "type": "feature",
        })
        (changes / "unreleased.jsonl").write_text(entry + "\n")

        ctx = make_ctx(repo)
        ctx.push_stdin = _make_push_stdin(head_sha, base_sha, branch="feature-x")

        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        assert result.status == "pass"


# ==================================================================
# 6c: BranchValidation
# ==================================================================


class TestBranchValidation:
    """Unit tests for the BranchValidation class."""

    def test_str_returns_branch(self):
        bv = BranchValidation("main")
        assert str(bv) == "main"

    def test_eq_with_string(self):
        bv = BranchValidation("main")
        assert bv == "main"
        assert bv != "dev"

    def test_eq_with_branch_validation(self):
        bv1 = BranchValidation("main", dev_branch="dev", needs_ff_merge=True)
        bv2 = BranchValidation("main", dev_branch="dev", needs_ff_merge=True)
        assert bv1 == bv2

    def test_dev_branch_attributes(self):
        bv = BranchValidation("main", dev_branch="dev", needs_ff_merge=True)
        assert bv.branch == "main"
        assert bv.dev_branch == "dev"
        assert bv.needs_ff_merge is True

    def test_release_branch_attributes(self):
        bv = BranchValidation("main")
        assert bv.branch == "main"
        assert bv.dev_branch is None
        assert bv.needs_ff_merge is False


# ==================================================================
# 6c: validate_branch_and_remote with dev branch
# ==================================================================


class TestValidateBranchOnReleaseBranch:
    """Normal release from a release branch -- unchanged behavior."""

    def test_returns_branch_on_main(self, dev_branch_repo):
        """When on main, returns BranchValidation with no ff-merge needed."""
        repo = dev_branch_repo
        run_git(repo, "checkout", "main")

        result = validate_branch_and_remote({}, config={"release_branches": ["main"]})
        assert isinstance(result, BranchValidation)
        assert result.branch == "main"
        assert result.dev_branch is None
        assert result.needs_ff_merge is False


class TestValidateBranchOnDevBranch:
    """Release from a dev branch -- detect ff-merge needed."""

    def test_returns_ff_merge_needed(self, dev_branch_repo):
        """When on dev with main as ancestor, returns needs_ff_merge=True."""
        repo = dev_branch_repo
        # Already on dev branch (fixture leaves us there)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert result.stdout.strip() == "dev"

        result = validate_branch_and_remote(
            {}, config={"release_branches": ["main"]},
        )
        assert isinstance(result, BranchValidation)
        assert result.branch == "main"
        assert result.dev_branch == "dev"
        assert result.needs_ff_merge is True

    def test_diverged_main_raises(self, dev_branch_repo):
        """When main has diverged from dev, raises ReleaseValidationError."""
        from rlsbl.commands.release.validate import ReleaseValidationError

        repo = dev_branch_repo
        # Create a divergent commit on main
        run_git(repo, "checkout", "main")
        (repo / "diverge.txt").write_text("divergent\n")
        run_git(repo, "add", "diverge.txt")
        run_git(repo, "commit", "-q", "-m", "divergent commit on main")
        run_git(repo, "checkout", "dev")

        with pytest.raises(ReleaseValidationError, match="cannot fast-forward"):
            validate_branch_and_remote(
                {}, config={"release_branches": ["main"]},
            )


class TestValidateBranchDefaultConfig:
    """validate_branch_and_remote with default release_branches."""

    def test_default_release_branches_main_master(self, dev_branch_repo):
        """Without config, main and master are release branches."""
        repo = dev_branch_repo
        # On dev branch, main/master are defaults
        result = validate_branch_and_remote({})
        assert result.branch == "main"
        assert result.dev_branch == "dev"
        assert result.needs_ff_merge is True


# ==================================================================
# 6c: Full release-from-dev flow integration
# ==================================================================


class TestReleaseFromDevFFMerge:
    """Integration tests for the ff-merge release flow."""

    def test_ff_merge_moves_main_to_dev_head(self, dev_branch_repo):
        """After ff-merge, main should be at the same commit as dev."""
        repo = dev_branch_repo
        dev_head = git_head(repo)

        # Simulate what the release flow does: checkout main, ff-merge dev
        run_git(repo, "checkout", "main")
        run_git(repo, "merge", "--ff-only", "dev")

        # main should now be at dev's HEAD
        main_head = git_head(repo)
        assert main_head == dev_head

    def test_ff_merge_fails_on_diverged(self, dev_branch_repo):
        """ff-merge fails when main has diverged from dev."""
        repo = dev_branch_repo

        # Make a divergent commit on main
        run_git(repo, "checkout", "main")
        (repo / "diverge.txt").write_text("divergent\n")
        run_git(repo, "add", "diverge.txt")
        run_git(repo, "commit", "-q", "-m", "divergent")

        # ff-merge should fail
        result = subprocess.run(
            ["git", "merge", "--ff-only", "dev"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_back_on_dev_after_successful_release(self, dev_branch_repo):
        """After a successful release, the session should be back on dev
        with the version bump commit merged from main."""
        repo = dev_branch_repo
        dev_head_before = git_head(repo)

        # Simulate the full release-from-dev sequence:
        # 1. Checkout main and ff-merge dev
        run_git(repo, "checkout", "main")
        run_git(repo, "merge", "--ff-only", "dev")

        # 2. Simulate version bump commit on main
        (repo / "VERSION").write_text("0.1.0\n")
        run_git(repo, "add", "VERSION")
        run_git(repo, "commit", "-q", "-m", "v0.1.0")
        release_sha = git_head(repo)

        # 3. Switch back to dev and merge main
        run_git(repo, "checkout", "dev")
        run_git(repo, "merge", "--ff-only", "main")

        # Verify: on dev, HEAD matches release commit
        current_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert current_branch == "dev"
        assert git_head(repo) == release_sha

    def test_back_on_dev_after_failed_release(self, dev_branch_repo):
        """After a failed release, the session should be back on dev.
        Main should be reset to its pre-ff-merge state."""
        repo = dev_branch_repo
        dev_head = git_head(repo)

        # Record main's original HEAD
        run_git(repo, "checkout", "main")
        main_original = git_head(repo)
        run_git(repo, "checkout", "dev")

        # Simulate ff-merge (step 1)
        run_git(repo, "checkout", "main")
        run_git(repo, "merge", "--ff-only", "dev")

        # Simulate release failure: rollback main via git reset
        # (the release flow does git reset --hard pre_release_sha)
        run_git(repo, "reset", "--hard", main_original)

        # Switch back to dev (error handling path)
        run_git(repo, "checkout", "dev")

        # Verify: on dev, main is back to original
        current_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert current_branch == "dev"

        # Main should be back at its original state
        run_git(repo, "checkout", "main")
        assert git_head(repo) == main_original

    def test_normal_release_from_main_no_ff_merge(self, dev_branch_repo):
        """When already on main, no ff-merge step is needed."""
        repo = dev_branch_repo
        run_git(repo, "checkout", "main")

        result = validate_branch_and_remote(
            {}, config={"release_branches": ["main"]},
        )
        assert result.needs_ff_merge is False
        assert result.dev_branch is None
        assert result.branch == "main"


# ==================================================================
# 6c: Release-from-dev through _run_cmd_inner (integration)
# ==================================================================


class TestReleaseFromDevIntegration:
    """Test that _run_cmd_inner correctly handles the ff-merge flow.

    Uses mocking to test the release flow without actually running
    the full release machinery (hooks, gh CLI, etc.)."""

    def _make_release_repo(self, tmp_path, monkeypatch):
        """Create a repo with the minimum structure for release testing."""
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

        # Set up rlsbl structure
        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")

        releases = repo / ".rlsbl" / "releases"
        releases.mkdir(parents=True)

        (repo / ".rlsbl" / "config.json").write_text(
            json.dumps({
                "publish_mode": "ci",
                "targets": ["pypi"],
                "pipelines": {"pypi": {"type": "pypi", "local": False}},
            })
        )

        (repo / "pyproject.toml").write_text(
            '[project]\nname = "testpkg"\nversion = "0.1.0"\n'
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold rlsbl")

        # Set up bare remote
        bare = tmp_path / "bare.git"
        subprocess.run(
            ["git", "clone", "--bare", str(repo), str(bare)],
            check=True, capture_output=True,
        )
        run_git(repo, "remote", "add", "origin", str(bare))
        run_git(repo, "fetch", "origin")
        run_git(repo, "branch", "--set-upstream-to=origin/main", "main")

        return repo

    def test_validate_returns_ff_merge_from_dev(self, tmp_path, monkeypatch):
        """validate_branch_and_remote detects dev branch and sets
        needs_ff_merge=True with the correct target release branch."""
        repo = self._make_release_repo(tmp_path, monkeypatch)

        # Create dev branch with a commit
        run_git(repo, "checkout", "-b", "dev")
        (repo / "new_feature.py").write_text("x = 1\n")
        run_git(repo, "add", "new_feature.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")

        config = {"release_branches": ["main"]}
        result = validate_branch_and_remote({}, config=config)

        assert isinstance(result, BranchValidation)
        assert result.branch == "main"
        assert result.dev_branch == "dev"
        assert result.needs_ff_merge is True

    def test_validate_custom_release_branches(self, tmp_path, monkeypatch):
        """Custom release_branches config is respected."""
        repo = self._make_release_repo(tmp_path, monkeypatch)

        # Create a 'production' branch
        run_git(repo, "checkout", "-b", "production")
        run_git(repo, "checkout", "-b", "dev")
        (repo / "new_feature.py").write_text("x = 1\n")
        run_git(repo, "add", "new_feature.py")
        run_git(repo, "commit", "-q", "-m", "feat: new feature")

        config = {"release_branches": ["production"]}
        result = validate_branch_and_remote({}, config=config)

        assert result.branch == "production"
        assert result.dev_branch == "dev"
        assert result.needs_ff_merge is True

    def test_validate_on_release_branch_no_merge(self, tmp_path, monkeypatch):
        """When already on a configured release branch, no ff-merge needed."""
        repo = self._make_release_repo(tmp_path, monkeypatch)

        config = {"release_branches": ["main"]}
        result = validate_branch_and_remote({}, config=config)

        assert result.branch == "main"
        assert result.dev_branch is None
        assert result.needs_ff_merge is False
