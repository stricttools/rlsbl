"""Tests for the migration of doctor checks to the strictcli check system."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from strictcli import CheckResult

from rlsbl import app
from rlsbl.check_context import ProjectCheckContext


# ---------------------------------------------------------------------------
# All 11 checks are declared in checks.toml and registered on the app
# ---------------------------------------------------------------------------

EXPECTED_CHECKS = [
    "lock",
    "version-consistency",
    "name-consistency",
    "license-consistency",
    "description-consistency",
    "local-tag",
    "remote-tag",
    "github-release",
    "branch-sync",
    "changelog-entry",
    "library-lint",
]


class TestCheckDeclarations:
    """Every check must be declared in checks.toml and registered on the app."""

    def test_all_11_checks_declared(self):
        """checks.toml defines exactly the 11 expected checks."""
        assert sorted(app._check_defs.keys()) == sorted(EXPECTED_CHECKS)

    @pytest.mark.parametrize("name", EXPECTED_CHECKS)
    def test_check_has_implementation(self, name):
        """Each declared check has a non-None implementation function."""
        assert app._check_defs[name].impl is not None

    def test_check_list_shows_all(self):
        """``rlsbl check --list`` outputs all 11 check names."""
        result = app.test(["check", "--list"])
        assert result.exit_code == 0
        for name in EXPECTED_CHECKS:
            assert name in result.stdout

    def test_check_list_json(self):
        """``rlsbl check --list --json`` outputs valid JSON with all 11 checks."""
        result = app.test(["check", "--list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        names = [item["name"] for item in data]
        assert sorted(names) == sorted(EXPECTED_CHECKS)


# ---------------------------------------------------------------------------
# Tag assignments
# ---------------------------------------------------------------------------

class TestCheckTags:
    """Checks have the correct tags."""

    @pytest.mark.parametrize("name", [
        "lock", "version-consistency", "name-consistency",
        "license-consistency", "description-consistency",
    ])
    def test_project_tag(self, name):
        assert "project" in app._check_defs[name].tags

    @pytest.mark.parametrize("name", [
        "local-tag", "remote-tag", "github-release", "branch-sync",
    ])
    def test_release_tag(self, name):
        assert "release" in app._check_defs[name].tags

    def test_changelog_tag(self):
        assert "changelog" in app._check_defs["changelog-entry"].tags

    def test_library_lint_quality_tag(self):
        assert "quality" in app._check_defs["library-lint"].tags


# ---------------------------------------------------------------------------
# Dependency chains
# ---------------------------------------------------------------------------

class TestCheckDependencies:
    """Dependency chains are correctly declared."""

    def test_local_tag_depends_on_version_consistency(self):
        assert "version-consistency" in app._check_defs["local-tag"].depends_on

    def test_remote_tag_depends_on_local_tag(self):
        assert "local-tag" in app._check_defs["remote-tag"].depends_on

    def test_github_release_depends_on_remote_tag(self):
        assert "remote-tag" in app._check_defs["github-release"].depends_on

    def test_changelog_entry_depends_on_version_consistency(self):
        assert "version-consistency" in app._check_defs["changelog-entry"].depends_on


# ---------------------------------------------------------------------------
# Functional tests: lock check
# ---------------------------------------------------------------------------

class TestLockCheck:
    """The lock check detects stale lock files."""

    def test_lock_pass_no_lock_file(self, mock_git_repo):
        """No lock file -> pass."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["lock"].impl(ctx)
        assert result.status == "pass"
        assert "no lock file" in result.message

    def test_lock_warn_stale(self, mock_git_repo):
        """Stale lock file -> warn."""
        lock_dir = mock_git_repo / ".rlsbl"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("")

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["lock"].impl(ctx)
        assert result.status == "warn"
        assert "stale" in result.message


# ---------------------------------------------------------------------------
# Functional tests: version-consistency check
# ---------------------------------------------------------------------------

class TestVersionConsistencyCheck:
    """The version-consistency check compares target versions."""

    def test_version_pass_single_target(self, mock_git_repo):
        """Single npm target with version -> pass."""
        pkg = {"name": "test-pkg", "version": "1.2.3"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.2.3" in result.message

    def test_version_warn_no_targets(self, mock_git_repo):
        """No targets detected -> warn."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets" in result.message


# ---------------------------------------------------------------------------
# Functional tests: changelog-entry check
# ---------------------------------------------------------------------------

class TestChangelogEntryCheck:
    """The changelog-entry check verifies CHANGELOG.md has the current version."""

    def test_changelog_pass(self, mock_git_repo):
        """CHANGELOG.md with matching version -> pass."""
        pkg = {"name": "test-pkg", "version": "2.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        (mock_git_repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 2.0.0\n\nSome changes.\n"
        )

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "pass"
        assert "2.0.0" in result.message

    def test_changelog_warn_no_file(self, mock_git_repo):
        """No CHANGELOG.md -> warn."""
        pkg = {"name": "test-pkg", "version": "2.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "warn"

    def test_changelog_skip_no_version(self, mock_git_repo):
        """No version detected -> skip."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# Functional tests: local-tag check
# ---------------------------------------------------------------------------

class TestLocalTagCheck:
    """The local-tag check verifies the git tag exists."""

    def test_local_tag_pass(self, mock_git_repo):
        """Tag exists locally -> pass."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        subprocess.run(
            ["git", "tag", "v1.0.0"],
            cwd=str(mock_git_repo),
            check=True,
        )

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "pass"
        assert "v1.0.0" in result.message

    def test_local_tag_warn(self, mock_git_repo):
        """Tag does not exist -> warn."""
        pkg = {"name": "test-pkg", "version": "9.9.9"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "warn"
        assert "not found" in result.message

    def test_local_tag_skip_no_version(self, mock_git_repo):
        """No version detected -> skip."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# Functional tests: branch-sync check
# ---------------------------------------------------------------------------

class TestBranchSyncCheck:
    """The branch-sync check compares local and remote branches."""

    def test_branch_sync_warn_no_remote(self, mock_git_repo):
        """No remote tracking branch -> warn."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "warn"
        assert "no remote tracking" in result.message


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestCheckDryRun:
    """``check --all --dry-run`` previews which checks would run."""

    def test_dry_run_lists_all_checks(self):
        result = app.test(["check", "--all", "--dry-run"])
        assert result.exit_code == 0
        for name in EXPECTED_CHECKS:
            assert name in result.stdout


# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------

class TestCheckTagFiltering:
    """``check --tag <expr>`` filters checks by tag."""

    def test_tag_project(self):
        result = app.test(["check", "--tag", "project", "--dry-run"])
        assert result.exit_code == 0
        assert "lock" in result.stdout
        assert "version-consistency" in result.stdout
        # release-only checks should not appear
        assert "branch-sync" not in result.stdout

    def test_tag_release(self):
        result = app.test(["check", "--tag", "release", "--dry-run"])
        assert result.exit_code == 0
        assert "local-tag" in result.stdout
        assert "branch-sync" in result.stdout
        # project-only checks should not appear
        assert "lock" not in result.stdout

    def test_tag_changelog(self):
        result = app.test(["check", "--tag", "changelog", "--dry-run"])
        assert result.exit_code == 0
        assert "changelog-entry" in result.stdout
