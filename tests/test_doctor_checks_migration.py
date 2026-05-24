"""Tests for the migration of doctor checks to the strictcli check system."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from strictcli import CheckResult

from rlsbl import app
from rlsbl.check_context import ProjectCheckContext, WorkspaceCheckContext


# ---------------------------------------------------------------------------
# All 29 checks are declared in checks.toml and registered on the app
# ---------------------------------------------------------------------------

EXPECTED_CHECKS = [
    "lock",
    "version-consistency",
    "name-consistency",
    "license-consistency",
    "description-consistency",
    "private-hook-stale",
    "config-schema",
    "local-tag",
    "remote-tag",
    "github-release",
    "branch-sync",
    "changelog-entry",
    "library-lint",
    # Changelog validation checks
    "changelog-hashes",
    "changelog-range",
    "changelog-coverage",
    "changelog-orphans",
    "changelog-schema",
    "changelog-batch-commits",
    "changelog-batch-entries",
    # Workspace checks
    "workspace-ci-router",
    "workspace-ci-synced",
    "workspace-targets",
    "workspace-unregistered",
    "workspace-stale-entries",
    # Layer checks
    "layers-violations",
    # Dependency validation checks
    "deps-unused",
    "deps-undeclared",
    "deps-stale",
]


class TestCheckDeclarations:
    """Every check must be declared in checks.toml and registered on the app."""

    def test_all_28_checks_declared(self):
        """checks.toml defines exactly the 29 expected checks."""
        assert sorted(app._check_defs.keys()) == sorted(EXPECTED_CHECKS)

    @pytest.mark.parametrize("name", EXPECTED_CHECKS)
    def test_check_has_implementation(self, name):
        """Each declared check has a non-None implementation function."""
        assert app._check_defs[name].impl is not None

    def test_check_list_shows_all(self):
        """``rlsbl check --list`` outputs all 28 check names."""
        result = app.test(["check", "--list"])
        assert result.exit_code == 0
        for name in EXPECTED_CHECKS:
            assert name in result.stdout

    def test_check_list_json(self):
        """``rlsbl check --list --json`` outputs valid JSON with all 29 checks."""
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
        "private-hook-stale", "config-schema",
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

    def test_docs_target_included_in_consistency(self, mock_git_repo):
        """DocsTarget is a versioned target and participates in consistency.

        When selfdoc.json has the same version as package.json, the check
        passes across both targets.
        """
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        # selfdoc.json with matching version
        (mock_git_repo / "selfdoc.json").write_text(
            json.dumps({"version": "1.0.0"})
        )

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.0.0" in result.message

    def test_docs_target_mismatch_detected(self, mock_git_repo):
        """DocsTarget version mismatch with other targets causes failure."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        # selfdoc.json with different version
        (mock_git_repo / "selfdoc.json").write_text(
            json.dumps({"version": "0.5.0"})
        )

        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "fail"
        assert "mismatch" in result.message


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
        # New changelog validation checks should also appear
        assert "changelog-hashes" in result.stdout
        assert "changelog-range" in result.stdout
        assert "changelog-coverage" in result.stdout
        assert "changelog-orphans" in result.stdout
        assert "changelog-schema" in result.stdout
        assert "changelog-batch-commits" in result.stdout
        assert "changelog-batch-entries" in result.stdout

    def test_tag_workspace(self):
        result = app.test(["check", "--tag", "workspace", "--dry-run"])
        assert result.exit_code == 0
        assert "workspace-ci-router" in result.stdout
        assert "workspace-ci-synced" in result.stdout
        assert "workspace-targets" in result.stdout
        assert "workspace-unregistered" in result.stdout
        assert "workspace-stale-entries" in result.stdout


# ---------------------------------------------------------------------------
# Dependency chains for new checks
# ---------------------------------------------------------------------------

class TestNewCheckDependencies:
    """Dependency chains for changelog and workspace checks."""

    def test_changelog_range_depends_on_hashes(self):
        assert "changelog-hashes" in app._check_defs["changelog-range"].depends_on

    def test_changelog_coverage_depends_on_hashes(self):
        assert "changelog-hashes" in app._check_defs["changelog-coverage"].depends_on

    def test_changelog_orphans_no_deps(self):
        assert app._check_defs["changelog-orphans"].depends_on == []

    def test_changelog_schema_no_deps(self):
        assert app._check_defs["changelog-schema"].depends_on == []

    def test_workspace_checks_no_deps(self):
        for name in [
            "workspace-ci-router", "workspace-ci-synced",
            "workspace-targets", "workspace-unregistered",
            "workspace-stale-entries",
        ]:
            assert app._check_defs[name].depends_on == []


# ---------------------------------------------------------------------------
# Functional tests: changelog-schema check
# ---------------------------------------------------------------------------

class TestChangelogSchemaCheck:
    """The changelog-schema check validates entry structure."""

    def test_schema_pass_valid_entries(self, mock_git_repo):
        """Valid entries -> pass."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        # Need a real commit hash for the entry
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(mock_git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        (changes / "unreleased.jsonl").write_text(
            f'{{"commits":["{head}"],"user_facing":true,"description":"A feature.","type":"feature"}}\n'
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "pass"

    def test_schema_fail_missing_description(self, mock_git_repo):
        """user_facing entry without description -> fail."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(mock_git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        (changes / "unreleased.jsonl").write_text(
            f'{{"commits":["{head}"],"user_facing":true}}\n'
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) > 0

    def test_schema_skip_no_changes_dir(self, mock_git_repo):
        """No .rlsbl/changes/ -> skip."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "skip"

    def test_schema_pass_empty_unreleased(self, mock_git_repo):
        """Empty unreleased.jsonl -> pass (no entries to validate)."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# Functional tests: changelog-hashes check
# ---------------------------------------------------------------------------

class TestChangelogHashesCheck:
    """The changelog-hashes check validates that commit hashes resolve."""

    def test_hashes_pass(self, mock_git_repo):
        """Valid hash -> pass."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(mock_git_repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        (changes / "unreleased.jsonl").write_text(
            f'{{"commits":["{head[:7]}"],"user_facing":false}}\n'
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "pass"

    def test_hashes_fail_bad_hash(self, mock_git_repo):
        """Bogus hash -> fail."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(
            '{"commits":["deadbeef0000000"],"user_facing":false}\n'
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) > 0


# ---------------------------------------------------------------------------
# Functional tests: workspace checks
# ---------------------------------------------------------------------------

class TestWorkspaceChecksSkipForNonWorkspace:
    """Workspace checks return skip when context is not a WorkspaceCheckContext."""

    @pytest.mark.parametrize("name", [
        "workspace-ci-router",
        "workspace-ci-synced",
        "workspace-targets",
        "workspace-unregistered",
        "workspace-stale-entries",
    ])
    def test_skip_for_project_context(self, mock_git_repo, name):
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs[name].impl(ctx)
        assert result.status == "skip"
        assert "not a monorepo" in result.message


class TestWorkspaceCiRouterCheck:
    """The workspace-ci-router check verifies ci-router.yml exists."""

    def test_ci_router_pass(self, mock_git_repo):
        """ci-router.yml exists -> pass."""
        workflows = mock_git_repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci-router.yml").write_text("name: CI Router\n")
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            projects=[],
            graph=None,
        )
        result = app._check_defs["workspace-ci-router"].impl(ctx)
        assert result.status == "pass"

    def test_ci_router_fail(self, mock_git_repo):
        """ci-router.yml missing -> fail."""
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            projects=[],
            graph=None,
        )
        result = app._check_defs["workspace-ci-router"].impl(ctx)
        assert result.status == "fail"


class TestWorkspaceStaleEntriesCheck:
    """The workspace-stale-entries check finds entries pointing to missing dirs."""

    def test_stale_entries_pass(self, mock_git_repo):
        """All registered projects have dirs with manifests -> pass."""
        proj_dir = mock_git_repo / "mylib"
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text('{"name":"mylib","version":"1.0.0"}')
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            projects=[{"path": "mylib", "name": "mylib"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "pass"

    def test_stale_entries_fail_missing_dir(self, mock_git_repo):
        """Registered project dir doesn't exist -> fail."""
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            projects=[{"path": "nonexistent", "name": "ghost"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) == 1


# ---------------------------------------------------------------------------
# Functional tests: private-hook-stale check
# ---------------------------------------------------------------------------

class TestPrivateHookStaleCheck:
    """The private-hook-stale check detects legacy private hook code."""

    def test_stale_hook_detected(self, mock_git_repo):
        """Hook containing legacy private marker -> fail."""
        hooks_dir = mock_git_repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# Post-release hook for private repositories.\n"
            "gh release upload \"v$version\" ./dist/* --clobber\n"
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "fail"
        assert "legacy private asset upload" in result.message
        assert "rlsbl scaffold --update" in result.message

    def test_normal_hook_passes(self, mock_git_repo):
        """Standard hook without legacy marker -> pass."""
        hooks_dir = mock_git_repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# Built-in checks handle tests and lint.\n"
            "echo 'post-release'\n"
        )
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "pass"

    def test_no_hook_file_passes(self, mock_git_repo):
        """No post-release.sh at all -> pass."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "pass"
        assert "no post-release hook" in result.message


# ---------------------------------------------------------------------------
# Functional tests: library-lint check
# ---------------------------------------------------------------------------

class TestLibraryLintCheck:
    """The library-lint check skips non-library projects."""

    def test_standalone_project_skips(self, mock_git_repo):
        """Standalone (non-monorepo) project -> skip."""
        ctx = ProjectCheckContext(project_root=mock_git_repo)
        result = app._check_defs["library-lint"].impl(ctx)
        assert result.status == "skip"
        assert "not a library" in result.message
