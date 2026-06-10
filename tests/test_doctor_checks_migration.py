"""Tests for the migration of doctor checks to the strictcli check system."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from strictcli import CheckResult

from rlsbl import app
from rlsbl.context import ProjectContext
from rlsbl.check_context import WorkspaceCheckContext


# ---------------------------------------------------------------------------
# All checks are declared in checks.toml and registered on the app
# ---------------------------------------------------------------------------

EXPECTED_CHECKS = [
    "lock",
    "version-consistency",
    "name-consistency",
    "license-consistency",
    "description-consistency",
    "private-hook-stale",
    "config-schema",
    "license-file",
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
    "changelog-user-facing",
    "changelog-batch-commits",
    "changelog-batch-entries",
    # Workspace checks
    "workspace-ci-router",
    "workspace-ci-synced",
    "workspace-targets",
    "workspace-unregistered",
    "workspace-stale-entries",
    "dev-node-boundary",
    # Layer checks
    "layers-violations",
    # Dependency validation checks
    "deps-unused",
    "deps-undeclared",
    "deps-stale",
    # Quality checks
    "deps-runtime-test-only",
    "deps-dev-in-lib",
    "dead-modules",
    "scaffold-unreplaced-vars",
    "scaffold-conflict-markers",
    "dead-workspace-packages",
    "subtree-remote-reachable",
    "circular-deps",
    # Phase 12 project checks
    "private-publish-workflow",
    "npm-private-mismatch",
    "target-version-readable",
    "selfdoc-version-drift",
    # Pre-push checks
    "prepush-changelog-coverage",
    "prepush-gitignore-guard",
    "prepush-manual-warning",
    "test-suite",
]


class TestCheckDeclarations:
    """Every check must be declared in checks.toml and registered on the app."""

    def test_all_checks_declared(self):
        """checks.toml defines exactly the expected checks."""
        assert sorted(app._check_defs.keys()) == sorted(EXPECTED_CHECKS)
        assert len(app._check_defs) == len(EXPECTED_CHECKS)

    @pytest.mark.parametrize("name", EXPECTED_CHECKS)
    def test_check_has_implementation(self, name):
        """Each declared check has a non-None implementation function."""
        assert app._check_defs[name].impl is not None

    def test_check_list_shows_all(self):
        """``rlsbl check --list`` outputs all check names."""
        result = app.test(["check", "--list"])
        assert result.exit_code == 0
        for name in EXPECTED_CHECKS:
            assert name in result.stdout

    def test_check_list_json(self):
        """``rlsbl check --list --json`` outputs valid JSON with all checks."""
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
        "private-hook-stale", "config-schema", "license-file",
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

    @pytest.mark.parametrize("name", [
        "library-lint", "deps-runtime-test-only", "deps-dev-in-lib",
        "dead-modules", "scaffold-unreplaced-vars",
    ])
    def test_quality_tag(self, name):
        assert "quality" in app._check_defs[name].tags


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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["lock"].impl(ctx)
        assert result.status == "pass"
        assert "no lock file" in result.message

    def test_lock_warn_stale(self, mock_git_repo):
        """Stale lock file -> warn."""
        lock_dir = mock_git_repo / ".rlsbl"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "lock").write_text("")

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.2.3" in result.message

    def test_version_warn_no_targets(self, mock_git_repo):
        """No targets detected -> warn."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "warn"
        assert "no targets" in result.message

    def test_selfdoc_included_in_consistency(self, mock_git_repo):
        """selfdoc.json is included in version-consistency checks.

        When selfdoc.json has the same version as package.json, the check
        passes across both sources.
        """
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        # selfdoc.json with matching version
        (mock_git_repo / "selfdoc.json").write_text(
            json.dumps({"version": "1.0.0"})
        )

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "pass"
        assert "1.0.0" in result.message

    def test_selfdoc_version_mismatch_detected(self, mock_git_repo):
        """selfdoc.json version mismatch with other targets causes failure."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        # selfdoc.json with different version
        (mock_git_repo / "selfdoc.json").write_text(
            json.dumps({"version": "0.5.0"})
        )

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "pass"
        assert "2.0.0" in result.message

    def test_changelog_warn_no_file(self, mock_git_repo):
        """No CHANGELOG.md -> warn."""
        pkg = {"name": "test-pkg", "version": "2.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "warn"

    def test_changelog_skip_no_version(self, mock_git_repo):
        """No version detected -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "pass"
        assert "v1.0.0" in result.message

    def test_local_tag_warn(self, mock_git_repo):
        """Tag does not exist -> warn."""
        pkg = {"name": "test-pkg", "version": "9.9.9"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "warn"
        assert "not found" in result.message

    def test_local_tag_skip_no_version(self, mock_git_repo):
        """No version detected -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["local-tag"].impl(ctx)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# Functional tests: branch-sync check
# ---------------------------------------------------------------------------

class TestBranchSyncCheck:
    """The branch-sync check compares local and remote branches."""

    def test_branch_sync_warn_no_remote(self, mock_git_repo):
        """No remote tracking branch -> warn."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
        assert "dev-node-boundary" in result.stdout


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
            "workspace-stale-entries", "dev-node-boundary",
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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) > 0

    def test_schema_skip_no_changes_dir(self, mock_git_repo):
        """No .rlsbl/changes/ -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-schema"].impl(ctx)
        assert result.status == "skip"

    def test_schema_pass_empty_unreleased(self, mock_git_repo):
        """Empty unreleased.jsonl -> pass (no entries to validate)."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-hashes"].impl(ctx)
        assert result.status == "pass"

    def test_hashes_fail_bad_hash(self, mock_git_repo):
        """Bogus hash -> fail."""
        changes = mock_git_repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(
            '{"commits":["deadbeef0000000"],"user_facing":false}\n'
        )
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
            config={},
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
            config={},
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
            config={},
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
            config={},
            projects=[{"path": "nonexistent", "name": "ghost"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "fail"
        assert len(result.details) == 1

    def test_dart_project_not_stale(self, mock_git_repo):
        """Dart project with pubspec.yaml is NOT flagged as stale."""
        proj_dir = mock_git_repo / "flutter_app"
        proj_dir.mkdir()
        (proj_dir / "pubspec.yaml").write_text("name: flutter_app\nversion: 1.0.0\n")
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "flutter_app", "name": "flutter_app"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "pass"

    def test_rlsbl_config_not_stale(self, mock_git_repo):
        """Project with .rlsbl/config.json but no traditional manifest is NOT stale."""
        proj_dir = mock_git_repo / "custom_proj"
        proj_dir.mkdir()
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"private": false}')
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "custom_proj", "name": "custom_proj"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "pass"

    def test_plain_target_with_rlsbl_config_not_stale(self, mock_git_repo):
        """Plain-target project with VERSION and .rlsbl/config.json is NOT stale."""
        proj_dir = mock_git_repo / "plain_proj"
        proj_dir.mkdir()
        (proj_dir / "VERSION").write_text("1.0.0\n")
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text('{"private": false}')
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "plain_proj", "name": "plain_proj"}],
            graph=None,
        )
        result = app._check_defs["workspace-stale-entries"].impl(ctx)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# Functional tests: workspace-unregistered check
# ---------------------------------------------------------------------------

class TestWorkspaceUnregisteredCheck:
    """The workspace-unregistered check detects dirs with manifests not in workspace.toml."""

    def test_private_package_json_not_flagged(self, mock_git_repo):
        """Directory with private package.json is NOT flagged as unregistered."""
        # Create a private npm workspace root (e.g., pnpm workspace root)
        web_dir = mock_git_repo / "web"
        web_dir.mkdir()
        (web_dir / "package.json").write_text(json.dumps({
            "name": "web-workspace",
            "private": True,
            "workspaces": ["packages/*"],
        }))
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "pass"

    def test_non_private_package_json_flagged(self, mock_git_repo):
        """Directory with non-private package.json IS flagged as unregistered."""
        lib_dir = mock_git_repo / "mylib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(json.dumps({
            "name": "mylib",
            "version": "1.0.0",
        }))
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "fail"
        assert any("mylib" in d for d in result.details)

    def test_parent_of_registered_path_not_flagged(self, mock_git_repo):
        """Directory that is a parent of a registered project path is NOT flagged."""
        # Create web/ with a package.json (would normally be flagged)
        web_dir = mock_git_repo / "web"
        web_dir.mkdir()
        (web_dir / "package.json").write_text(json.dumps({
            "name": "web-root",
            "version": "1.0.0",
        }))
        # Register web/frontend as a project (making web/ a parent)
        frontend_dir = web_dir / "frontend"
        frontend_dir.mkdir()
        (frontend_dir / "package.json").write_text(json.dumps({
            "name": "frontend",
            "version": "1.0.0",
        }))
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "web/frontend", "name": "frontend"}],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "pass"

    def test_non_parent_dir_still_flagged(self, mock_git_repo):
        """Directory that is NOT a parent of any registered path IS flagged."""
        # Create two dirs: one registered, one not
        registered_dir = mock_git_repo / "backend"
        registered_dir.mkdir()
        (registered_dir / "pyproject.toml").write_text('[project]\nname = "backend"\n')

        unrelated_dir = mock_git_repo / "tools"
        unrelated_dir.mkdir()
        (unrelated_dir / "pyproject.toml").write_text('[project]\nname = "tools"\n')

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": "backend", "name": "backend"}],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "fail"
        assert any("tools" in d for d in result.details)
        # backend should NOT be in the unregistered list (it's registered)
        assert not any("backend" in d for d in result.details)


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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "fail"
        assert "legacy private asset upload" in result.message
        assert "rlsbl scaffold" in result.message

    def test_normal_hook_passes(self, mock_git_repo):
        """Standard hook without legacy marker -> pass."""
        hooks_dir = mock_git_repo / ".rlsbl" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "post-release.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# Built-in checks handle tests and lint.\n"
            "echo 'post-release'\n"
        )
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["private-hook-stale"].impl(ctx)
        assert result.status == "pass"

    def test_no_hook_file_passes(self, mock_git_repo):
        """No post-release.sh at all -> pass."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
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
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["library-lint"].impl(ctx)
        assert result.status == "skip"
        assert "not a library" in result.message
