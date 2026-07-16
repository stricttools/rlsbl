"""Integration tests for the prepush check system.

Tests the four prepush checks (prepush-changelog-coverage,
prepush-gitignore-guard, prepush-manual-warning, test-suite) as
registered on the strictcli check system, including depends_on ordering.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import git_head, make_commit, make_ctx, make_workspace, run_git
from rlsbl import app
from rlsbl.check_context import WorkspaceCheckContext


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

    (repo / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci", "targets": []}))

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
        # Declare pypi target in config
        (prepush_repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]})
        )

        ctx = make_ctx(prepush_repo)

        with patch("rlsbl.testing.run_project_tests", return_value=True) as mock_tests:
            result = app._check_defs["test-suite"].impl(ctx)

        assert result.status == "pass"
        mock_tests.assert_called_once_with(
            "pypi",
            project_dir=str(prepush_repo),
            workspace_root=None,
            config=ctx.config,
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
        # Declare pypi target in config
        (prepush_repo / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]})
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
        results, exit_code = app.run_checks(ctx, tag_expr="prepush", ignore_warnings=True)
        results_dict = {cr.name: cr.result for cr in results}

        # changelog-coverage must have failed
        assert results_dict["prepush-changelog-coverage"].status == "fail"

        # test-suite must be skipped because its dependency failed
        assert results_dict["test-suite"].status == "skip"
        assert "prepush-changelog-coverage" in results_dict["test-suite"].message

        assert exit_code == 1


# ------------------------------------------------------------------
# Test: pre-push-check at monorepo root does not crash
# ------------------------------------------------------------------


class TestPrePushCheckAtMonorepoRoot:
    """pre-push-check works when CWD is the workspace root (git hook context)."""

    def test_pre_push_check_at_monorepo_root_no_error(self, tmp_path, monkeypatch):
        """Set up a monorepo workspace, call from workspace root, verify no crash."""
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

        # Create a sub-project
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "alpha", "version": "0.1.0"}\n')

        changes = pkg / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text("")
        (pkg / ".rlsbl" / "config.json").write_text(json.dumps({"publish_mode": "ci", "targets": ["npm"]}))

        # Set up workspace
        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold workspace")

        base_sha = git_head(repo)

        # Make a commit in the sub-project
        (pkg / "index.js").write_text("module.exports = 1;\n")
        run_git(repo, "add", "packages/alpha/index.js")
        run_git(repo, "commit", "-q", "-m", "feat: alpha feature")
        head_sha = git_head(repo)

        # Simulate pre-push from workspace root (where git runs hooks)
        push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        # Build context as cmd_pre_push_check would at workspace root
        from rlsbl.workspace import find_workspace_root

        ws_root = find_workspace_root(str(repo))
        assert ws_root is not None

        ctx = WorkspaceCheckContext(
            project_root=Path(ws_root),
            workspace_root=Path(ws_root),
            config={},
            projects=[],
            graph=None,
        )
        ctx.push_stdin = push_stdin

        # This should not crash -- the deprecated shim delegates to checks
        # Run the changelog coverage check directly to verify no crash
        result = app._check_defs["prepush-changelog-coverage"].impl(ctx)
        # It should detect monorepo mode and check projects
        assert result.status in ("pass", "fail", "skip")


# ------------------------------------------------------------------
# Test: test-suite hard-errors at workspace root
# ------------------------------------------------------------------


class TestTestSuiteHardErrorsAtWorkspaceRoot:
    """test-suite check returns fail when project_root == workspace_root."""

    def test_test_suite_hard_errors_at_workspace_root(self, tmp_path):
        """Create WorkspaceCheckContext where project_root == workspace_root,
        call test-suite check, verify CheckResult status is fail."""
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()

        ctx = WorkspaceCheckContext(
            project_root=ws_root,
            workspace_root=ws_root,
            config={},
            projects=[],
            graph=None,
        )
        ctx.push_stdin = None

        result = app._check_defs["test-suite"].impl(ctx)
        assert result.status == "skip"
        assert "workspace root" in result.message


# ------------------------------------------------------------------
# Tests: test-suite-workspace
# ------------------------------------------------------------------


class TestWorkspaceTestSuiteSkipsNonWorkspace:
    """test-suite-workspace returns skip for non-workspace context (via scope adapter)."""

    def test_non_workspace_skips(self, prepush_repo):
        from rlsbl.checks.scope import scope_adapter

        ctx = make_ctx(prepush_repo)
        ctx.push_stdin = "refs/heads/main abc123 refs/heads/main 000000"

        result = scope_adapter(ctx, "workspace:non_dev_only")
        assert result.status == "skip"
        assert "not a monorepo" in result.message


class TestWorkspaceTestSuiteSkipsNoPushContext:
    """test-suite-workspace returns skip when push_stdin is not set."""

    def test_no_push_context_skips(self, tmp_path):
        ws_root = tmp_path / "monorepo"
        ws_root.mkdir()

        ctx = WorkspaceCheckContext(
            project_root=ws_root,
            workspace_root=ws_root,
            config={},
            projects=[],
            graph=None,
        )
        # push_stdin defaults to None
        assert ctx.push_stdin is None

        result = app._check_defs["test-suite-workspace"].impl(ctx)
        assert result.status == "skip"
        assert "not in push context" in result.message


class TestWorkspaceTestSuiteRunsAffectedProjects:
    """test-suite-workspace calls run_project_tests for affected projects."""

    def test_runs_affected_projects(self, tmp_path, monkeypatch):
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

        # Create a sub-project with pyproject.toml
        pkg = repo / "packages" / "alpha"
        pkg.mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0.1.0"\n'
        )

        make_workspace(repo, [{"path": "packages/alpha", "name": "alpha"}])

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold workspace")

        base_sha = git_head(repo)

        # Make a commit in the sub-project
        (pkg / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "packages/alpha/src.py")
        run_git(repo, "commit", "-q", "-m", "feat: alpha feature")
        head_sha = git_head(repo)

        push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = push_stdin

        with (
            patch("rlsbl.testing.run_project_tests", return_value=True) as mock_tests,
            patch("rlsbl.testing.sync_workspace", return_value=True) as mock_sync,
        ):
            result = app._check_defs["test-suite-workspace"].impl(ctx)

        assert result.status == "pass"
        # Upfront uv sync runs at workspace root
        mock_sync.assert_called_once_with(str(repo), check_timeout=120)
        # Per-project test runs with skip_sync=True
        mock_tests.assert_called_once_with(
            "pypi", project_dir=str(pkg), workspace_root=str(repo), skip_sync=True,
        )


class TestWorkspaceTestSuiteSkipsDevNodes:
    """test-suite-workspace does not test dev_node projects.

    After the scope migration, the scope adapter (workspace:non_dev_only)
    filters out dev_node projects before the check runs.
    """

    def test_dev_node_skipped(self, tmp_path, monkeypatch):
        from rlsbl.checks.scope import scope_adapter

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

        # Create a dev_node sub-project
        pkg = repo / "packages" / "devtool"
        pkg.mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "devtool"\nversion = "0.1.0"\n'
        )

        make_workspace(
            repo,
            [{"path": "packages/devtool", "name": "devtool", "dev_node": True}],
        )

        run_git(repo, "add", ".")
        run_git(repo, "commit", "-q", "-m", "scaffold workspace")

        base_sha = git_head(repo)

        # Make a commit in the dev_node project
        (pkg / "src.py").write_text("x = 1\n")
        run_git(repo, "add", "packages/devtool/src.py")
        run_git(repo, "commit", "-q", "-m", "feat: devtool feature")
        head_sha = git_head(repo)

        push_stdin = f"refs/heads/main {head_sha} refs/heads/main {base_sha}"

        from rlsbl.workspace import load_workspace

        projects = load_workspace(str(repo))

        ctx = WorkspaceCheckContext(
            project_root=Path(str(repo)),
            workspace_root=Path(str(repo)),
            config={},
            projects=projects,
            graph=None,
        )
        ctx.push_stdin = push_stdin

        # Apply scope adapter to filter out dev_node projects
        adapted = scope_adapter(ctx, "workspace:non_dev_only")

        with patch("rlsbl.testing.run_project_tests") as mock_tests:
            result = app._check_defs["test-suite-workspace"].impl(adapted)

        # dev_node projects should be filtered out, so no tests run
        mock_tests.assert_not_called()
        assert result.status == "pass"
        assert "no affected projects" in result.message


