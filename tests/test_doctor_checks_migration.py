"""Tests for the migration of doctor checks to the strictcli check system."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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
    "unpublished-refs",
    "branch-sync",
    # Networked release checks: CI credential, and the follow-ups a recorded
    # conversion still owes the outside world
    "npm-token-presence",
    "old-repo-archived",
    "go-deprecation-published",
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
    "changelog-format-version",
    "changelog-format-version-gate",
    # Workspace checks
    "router-filters-fresh",
    "workspace-ci-router",
    "workspace-ci-synced",
    "workspace-targets",
    "workspace-unregistered",
    "workspace-stale-entries",
    "dev-only-boundary",
    "unversioned-boundary",
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
    "dead-modules-stale",
    "scaffold-unreplaced-vars",
    "ruff-lint",
    "dead-workspace-packages",
    "subtree-remote-reachable",
    "workspace-unbuildable",
    "circular-deps",
    # Phase 12 project checks
    "publish-mode-workflow",
    "npm-private-mismatch",
    "target-version-readable",
    "dunder-version-missing",
    "selfdoc-version-drift",
    "scaffold-conflicts",
    "cross-repo-path-sources",
    "requires-services",
    "dev-overlay-drift",
    # Pre-push checks
    "prepush-changelog-coverage",
    "prepush-gitignore-guard",
    "prepush-manual-warning",
    "test-suite",
    "test-suite-workspace",
    "maven-central-metadata",
    # Scaffold checks
    "scaffold-gitignore-stale",
    # Root config conflict
    "root-rlsbl-conflict",
    # Go companion tags
    "go-companion-tags",
    # Releasable member residue
    "releasable-residue",
    # Member pytest rootdir-escape guard
    "member-pytest-config",
    # Mixed monorepo tag-scheme guard
    "mixed-tag-schemes",
    # Launcher pipeline checks
    "wrapper-producer",
    # strictspec certificate deploy gate
    "strictspec-certificate-gate",
    # stricttest floor adoption (sandboxed test runner)
    "stricttest-floor",
    # ecosystem-internal dependency floors (declared >= vs locked version)
    "dep-floors",
    # every lockfile still resolves the manifest beside it
    "dep-locks",
    # a go.mod module path names where the repository actually lives
    "go-module-identity",
    # the declared strictspec floor reaches every generated validator
    "strictspec-generated-floor",
    # committed target support matrix freshness (regenerate-and-compare)
    "target-matrix-fresh",
    # Path-capable tool checks and their competing-scope guards
    "lint",
    "lint-scope-guard",
    "format",
    "format-scope-guard",
    "type-check",
    "type-check-scope-guard",
]

# ``cli-test-coverage`` is a strictcli framework BUILT-IN check provider,
# auto-registered because the app is constructed with ``test_coverage=True``.
# It is NOT declared in checks.toml and is absent from ``app._check_defs``
# until the check providers are materialized (``_materialize_check_providers``),
# after which strictcli tracks it in ``app._provider_sourced_names``. It is
# asserted separately from the TOML-declared checks above.
# Provider-registered built-ins that ship with strictcli itself: the CLI
# test-coverage check plus the two the effects regime added.
BUILTIN_PROVIDER_CHECKS = [
    "cli-test-coverage",
    "effects-bypass",
    "observe-allowlist-breadth",
    # Warns when an escaping grant sits on a command that did not declare
    # itself consequential (strictcli's confirm redesign, contract 8.1/11).
    "consequential-grant-agreement",
]


class TestCheckDeclarations:
    """Every check must be declared in checks.toml and registered on the app."""

    def test_all_checks_declared(self):
        """checks.toml defines exactly the expected checks.

        Once check providers are materialized, ``app._check_defs`` also holds
        strictcli's built-in provider checks (e.g. ``cli-test-coverage``). Those
        are tracked in ``app._provider_sourced_names`` and excluded here; they
        are asserted separately in ``test_builtin_provider_checks_registered``.
        """
        app._materialize_check_providers()
        toml_checks = set(app._check_defs) - set(app._provider_sourced_names)
        assert sorted(toml_checks) == sorted(EXPECTED_CHECKS)
        assert len(toml_checks) == len(EXPECTED_CHECKS)

    def test_builtin_provider_checks_registered(self):
        """strictcli's built-in provider checks are registered with impls.

        ``cli-test-coverage`` is provided by the framework (the app is built
        with ``test_coverage=True``), not declared in checks.toml. After
        provider materialization it appears in both ``app._check_defs`` and
        ``app._provider_sourced_names``.
        """
        app._materialize_check_providers()
        for name in BUILTIN_PROVIDER_CHECKS:
            assert name in app._provider_sourced_names
            assert name in app._check_defs
            assert app._check_defs[name].impl is not None

    @pytest.mark.parametrize("name", EXPECTED_CHECKS)
    def test_check_has_implementation(self, name):
        """Each declared check has a non-None implementation function."""
        assert app._check_defs[name].impl is not None

    def test_check_list_shows_all(self):
        """``rlsbl check --list`` outputs all check names."""
        result = app.test(["check", "--list"])
        assert result.exit_code == 0
        for name in EXPECTED_CHECKS + BUILTIN_PROVIDER_CHECKS:
            assert name in result.stdout

    def test_check_list_json(self):
        """``rlsbl check --list --json`` outputs valid JSON with all checks."""
        result = app.test(["check", "--list", "--json"])
        assert result.exit_code == 0
        # Machine mode's stdout is strictcli's envelope (effects contract
        # §19.2); the check command's listing is its `payload` member.
        data = json.loads(result.stdout)["payload"]
        names = [item["name"] for item in data]
        assert sorted(names) == sorted(EXPECTED_CHECKS + BUILTIN_PROVIDER_CHECKS)


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
        "unpublished-refs", "branch-sync",
    ])
    def test_release_tag(self, name):
        assert "release" in app._check_defs[name].tags

    def test_changelog_tag(self):
        assert "changelog" in app._check_defs["changelog-entry"].tags

    @pytest.mark.parametrize("name", [
        "library-lint", "deps-runtime-test-only", "deps-dev-in-lib",
        "dead-modules", "dead-modules-stale", "scaffold-unreplaced-vars",
        "ruff-lint",
    ])
    def test_quality_tag(self, name):
        assert "quality" in app._check_defs[name].tags


# ---------------------------------------------------------------------------
# Dependency chains
# ---------------------------------------------------------------------------

class TestCheckDependencies:
    """Dependency chains are correctly declared."""

    def test_unpublished_refs_depends_on_version_consistency(self):
        assert "version-consistency" in app._check_defs["unpublished-refs"].depends_on

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

    def test_version_skip_no_targets(self, mock_git_repo):
        """No targets detected -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["version-consistency"].impl(ctx)
        assert result.status == "skip"
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

    def test_changelog_fail_no_file(self, mock_git_repo):
        """No CHANGELOG.md -> fail (error severity: missing file)."""
        pkg = {"name": "test-pkg", "version": "2.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "fail"

    def test_changelog_skip_no_version(self, mock_git_repo):
        """No version detected -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["changelog-entry"].impl(ctx)
        assert result.status == "skip"


# ---------------------------------------------------------------------------
# Functional tests: unpublished-refs check
# ---------------------------------------------------------------------------

class TestUnpublishedRefsCheck:
    """The successor of local-tag/remote-tag/github-release.

    Those three asked whether the CURRENT version's primary tag existed. This
    one asks the ledger which versions were released and renders every ref each
    of them owns against the repository. The two skip conditions the old checks
    had -- no version, nothing tagged yet -- are one condition here: the ledger
    records no release.
    """

    def test_skips_when_no_release_target(self, mock_git_repo):
        """Nothing to name refs for -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["unpublished-refs"].impl(ctx)
        assert result.status == "skip"

    def test_skips_when_the_ledger_records_nothing(self, mock_git_repo):
        """A project with a target but no archived release -> skip."""
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["unpublished-refs"].impl(ctx)
        assert result.status == "skip"
        assert "no release" in result.message

    def test_unreadable_archive_fails_the_check(self, mock_git_repo):
        """An archive that cannot be read is a FAILURE, not a pass.

        The per-version read error was reported to the reporter but counted
        nowhere, so the terminal decision saw zero findings and called
        ``passed()`` with problems already reported -- which the reporter
        refuses with a ValueError that takes the whole check run down.
        """
        pkg = {"name": "test-pkg", "version": "1.0.0"}
        (mock_git_repo / "package.json").write_text(json.dumps(pkg))
        releases = mock_git_repo / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        (releases / "v1.0.0.toml").write_text("this is not = valid = toml\n")

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["unpublished-refs"].impl(ctx)
        assert result.status == "fail"
        assert "1.0.0" in result.problems[0].text
        assert "1 release archive" in result.message


# ---------------------------------------------------------------------------
# Functional tests: branch-sync check
# ---------------------------------------------------------------------------

class TestBranchSyncCheck:
    """The branch-sync check compares local and remote branches."""

    def test_branch_sync_skip_no_remote(self, mock_git_repo):
        """No remote tracking branch -> skip."""
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["branch-sync"].impl(ctx)
        assert result.status == "skip"
        assert "no remote tracking" in result.message


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestCheckDryRun:
    """``check --all --dry-run`` previews which checks would run.

    Since strictcli unified the two preview behaviours, ``check --dry-run``
    EXECUTES the checks declared pure and renders only the impure remainder as
    the would-run plan -- so its exit code now reports real results. These tests
    run in a bare temp directory (stricttest chdirs every test into its own),
    which is not an rlsbl project, so pure checks legitimately fail there and
    the exit code is not the thing under test. What is under test is that every
    registered check appears in the preview, as an executed row or a plan entry.
    """

    def test_dry_run_lists_all_checks(self):
        result = app.test(["--dry-run", "check", "--all"])
        assert "Would run" in result.stdout
        for name in EXPECTED_CHECKS + BUILTIN_PROVIDER_CHECKS:
            assert name in result.stdout


# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------

class TestCheckTagFiltering:
    """``check --tag <expr>`` filters checks by tag.

    As in :class:`TestCheckDryRun`, a dry run now executes the pure checks, so
    in the bare temp CWD the exit code reflects those real results rather than
    the preview's success; the selection itself is what these assert.
    """

    def test_tag_project(self):
        result = app.test(["--dry-run", "check", "--tag", "project"])
        assert "Would run" in result.stdout
        assert "lock" in result.stdout
        assert "version-consistency" in result.stdout
        # release-only checks should not appear
        assert "branch-sync" not in result.stdout

    def test_tag_release(self):
        result = app.test(["--dry-run", "check", "--tag", "release"])
        assert result.exit_code == 0
        assert "unpublished-refs" in result.stdout
        assert "branch-sync" in result.stdout
        # project-only checks should not appear
        assert "lock" not in result.stdout

    def test_tag_changelog(self):
        result = app.test(["--dry-run", "check", "--tag", "changelog"])
        assert "Would run" in result.stdout
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
        result = app.test(["--dry-run", "check", "--tag", "workspace"])
        assert result.exit_code == 0
        assert "workspace-ci-router" in result.stdout
        assert "workspace-ci-synced" in result.stdout
        assert "workspace-targets" in result.stdout
        assert "workspace-unregistered" in result.stdout
        assert "workspace-stale-entries" in result.stdout
        assert "dev-only-boundary" in result.stdout
        assert "unversioned-boundary" in result.stdout


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
            "workspace-ci-router",
            "workspace-targets", "workspace-unregistered",
            "workspace-stale-entries", "dev-only-boundary",
            "unversioned-boundary",
        ]:
            assert app._check_defs[name].depends_on == []

    def test_workspace_ci_synced_depends_on_router(self):
        # workspace-ci-synced parses ci-router.yml, so it depends on the
        # check that verifies the router exists.
        assert app._check_defs["workspace-ci-synced"].depends_on == ["workspace-ci-router"]


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
        assert len(result.problems) > 0

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
        assert len(result.problems) > 0


# ---------------------------------------------------------------------------
# Functional tests: workspace checks
# ---------------------------------------------------------------------------

class TestWorkspaceChecksSkipForNonWorkspace:
    """Workspace checks return skip when context is not a WorkspaceCheckContext.

    After the scope migration, the scope adapter handles this via the
    ``workspace`` scope token. Tests verify through the adapter.
    """

    @pytest.mark.parametrize("name", [
        "workspace-ci-router",
        "workspace-ci-synced",
        "workspace-targets",
        "workspace-unregistered",
        "workspace-stale-entries",
    ])
    def test_skip_for_project_context(self, mock_git_repo, name):
        from strictcli import SkipCheck
        from rlsbl.checks.scope import scope_adapter

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        cdef = app._check_defs[name]
        result = scope_adapter(ctx, cdef.scope)
        assert isinstance(result, SkipCheck)
        assert "not a monorepo" in result.reason


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
        assert len(result.problems) == 1

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
        (rlsbl_dir / "config.json").write_text('{"publish_mode": "ci"}')
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
        (rlsbl_dir / "config.json").write_text('{"publish_mode": "ci"}')
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
        assert any("mylib" in p.text for p in result.problems)

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
        assert any("tools" in p.text for p in result.problems)
        # backend should NOT be in the unregistered list (it's registered)
        assert not any("backend" in p.text for p in result.problems)

    @staticmethod
    def _declare_member_targets(repo, targets):
        """Register the repo root as a member declaring *targets*."""
        rlsbl_dir = repo / ".rlsbl"
        rlsbl_dir.mkdir(exist_ok=True)
        (rlsbl_dir / "config.json").write_text(json.dumps({
            "publish_mode": "ci",
            "targets": targets,
        }))

    @staticmethod
    def _npm_wrapper(repo, name="npm"):
        """Create a subdirectory holding a publishable package.json."""
        wrapper = repo / name
        wrapper.mkdir()
        (wrapper / "package.json").write_text(json.dumps({
            "name": "wrapper",
            "version": "1.0.0",
        }))
        return wrapper

    def test_member_declared_target_path_not_flagged(self, mock_git_repo):
        """A directory declared as a member's target path is NOT flagged."""
        self._npm_wrapper(mock_git_repo)
        (mock_git_repo / "pyproject.toml").write_text('[project]\nname = "core"\n')
        self._declare_member_targets(
            mock_git_repo, ["pypi", {"name": "npm", "path": "npm"}]
        )
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": ".", "name": "core"}],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "pass", [p.text for p in result.problems]

    def test_releasable_declared_target_path_not_flagged(self, mock_git_repo):
        """A directory declared as a releasable's target path is NOT flagged."""
        self._npm_wrapper(mock_git_repo)
        (mock_git_repo / "pyproject.toml").write_text('[project]\nname = "core"\n')
        rel_dir = mock_git_repo / ".rlsbl-monorepo" / "releasables" / "core"
        rel_dir.mkdir(parents=True)
        (rel_dir / "config.json").write_text(json.dumps({
            "publish_mode": "ci",
            "targets": ["pypi", {"name": "npm", "path": "npm"}],
        }))
        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": ".", "name": "core", "releasable": "core"}],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "pass", [p.text for p in result.problems]

    def test_undeclared_dir_still_flagged_alongside_target_path(self, mock_git_repo):
        """Exempting declared target paths must not exempt everything else."""
        self._npm_wrapper(mock_git_repo)
        (mock_git_repo / "pyproject.toml").write_text('[project]\nname = "core"\n')
        self._declare_member_targets(
            mock_git_repo, ["pypi", {"name": "npm", "path": "npm"}]
        )
        tools_dir = mock_git_repo / "tools"
        tools_dir.mkdir()
        (tools_dir / "pyproject.toml").write_text('[project]\nname = "tools"\n')

        ctx = WorkspaceCheckContext(
            project_root=mock_git_repo,
            workspace_root=mock_git_repo,
            config={},
            projects=[{"path": ".", "name": "core"}],
            graph=None,
        )
        result = app._check_defs["workspace-unregistered"].impl(ctx)
        assert result.status == "fail"
        assert any("tools" in p.text for p in result.problems)
        assert not any("npm" in p.text for p in result.problems)


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
    """The library-lint check skips non-library projects.

    After the scope migration, library-lint uses scope ``workspace:library``
    which skips non-workspace contexts via the scope adapter.
    """

    def test_standalone_project_skips(self, mock_git_repo):
        """Standalone (non-monorepo) project -> skip (via scope adapter)."""
        from strictcli import SkipCheck
        from rlsbl.checks.scope import scope_adapter

        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = scope_adapter(ctx, "workspace:library")
        assert isinstance(result, SkipCheck)
        assert "not a monorepo" in result.reason


# ---------------------------------------------------------------------------
# Functional tests: ruff-lint check
# ---------------------------------------------------------------------------

def _make_pypi(repo):
    """Give *repo* a pypi target so ruff-lint applies."""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0.1.0"\n'
    )


class TestRuffLintCheck:
    """The ruff-lint check runs ruff against pypi-target projects."""

    def test_ruff_lint_pass_clean_project(self, mock_git_repo):
        """Clean Python file -> pass."""
        _make_pypi(mock_git_repo)
        (mock_git_repo / "clean.py").write_text("x = 1\n")
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "pass"
        assert "clean" in result.message

    def test_ruff_lint_skips_non_pypi(self, mock_git_repo):
        """No pypi target -> skip (ruff-lint is Python-only)."""
        (mock_git_repo / "bad.py").write_text("import os\n")
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "skip"
        assert "pypi" in result.message

    def test_ruff_lint_exact_violation_count(self, mock_git_repo):
        """Exactly two unused imports -> exactly two violations (JSON count,
        not the ~10x-inflated default-format line count)."""
        _make_pypi(mock_git_repo)
        (mock_git_repo / "bad.py").write_text("import os\nimport sys\n")
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "fail"
        assert "2 violation(s)" in result.message
        # Top rule codes and a fixable count are surfaced.
        assert "F401" in result.message
        assert "fixable" in result.message

    def test_ruff_lint_fail_reports_per_violation(self, mock_git_repo):
        """Each violation becomes an error line naming its file and rule code."""
        _make_pypi(mock_git_repo)
        (mock_git_repo / "bad.py").write_text("import os\n")
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "fail"
        assert any("F401" in p.text and "bad.py" in p.text for p in result.problems)

    def test_ruff_lint_fails_when_not_installed(self, mock_git_repo):
        """ruff not on PATH -> hard fail (a pypi project cannot be linted)."""
        _make_pypi(mock_git_repo)
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})
        with patch("rlsbl.utils.require_tool", return_value=None):
            result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "fail"
        assert "not installed" in result.message

    def test_ruff_lint_fails_below_version_floor(self, mock_git_repo):
        """A sub-floor ruff version -> hard fail with the floor in the message."""
        _make_pypi(mock_git_repo)
        ctx = ProjectContext(project_root=mock_git_repo, workspace_root=None, config={})

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["ruff", "--version"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="ruff 0.10.0\n", stderr="")
            raise AssertionError("ruff check must not run when version is below floor")

        with patch("subprocess.run", side_effect=fake_run):
            result = app._check_defs["ruff-lint"].impl(ctx)
        assert result.status == "fail"
        assert "0.15.20" in result.message
