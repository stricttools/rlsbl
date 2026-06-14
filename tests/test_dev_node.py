"""Tests for dev node projects skipping changelog infrastructure."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import run_git, make_commit, make_workspace
from rlsbl.commands.changelog_cmd import cmd_add
from rlsbl.commands.release import run_cmd as release_run_cmd
from rlsbl.commands.edit_release import run_cmd as edit_run_cmd
from rlsbl.context import ProjectContext
from rlsbl.release_file import ReleaseConfig
from rlsbl.workspace import WORKSPACE_DIR


@pytest.fixture
def dev_node_monorepo(tmp_path, monkeypatch):
    """Create a monorepo with one dev node and one regular project.

    Yields a SimpleNamespace with:
        root        -- Path to the repo root
        projects    -- list of project dicts (matching workspace.toml)
        dev_node_dir  -- absolute Path to the dev node subproject
        regular_dir  -- absolute Path to the regular subproject
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# monorepo test\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    projects = [
        {"path": "internal-pkg", "name": "mypkg-internal", "dev_node": True},
        {"path": "regular-pkg", "name": "mypkg-regular"},
    ]

    make_workspace(tmp_path, projects)

    dev_node_dir = tmp_path / "internal-pkg"
    regular_dir = tmp_path / "regular-pkg"

    # Set up dev node project with JSONL changelog
    (dev_node_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (dev_node_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (dev_node_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    (dev_node_dir / "package.json").write_text(
        json.dumps({"name": "mypkg-internal", "version": "0.1.0"})
    )

    # Set up regular project with JSONL changelog
    (regular_dir / ".rlsbl" / "changes").mkdir(parents=True)
    (regular_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (regular_dir / ".rlsbl" / "config.json").write_text(
        json.dumps({"private": False}) + "\n"
    )
    (regular_dir / "package.json").write_text(
        json.dumps({"name": "mypkg-regular", "version": "0.1.0"})
    )

    # Commit all
    run_git(tmp_path, "add", WORKSPACE_DIR)
    run_git(tmp_path, "add", "internal-pkg")
    run_git(tmp_path, "add", "regular-pkg")
    run_git(tmp_path, "commit", "-q", "-m", "add monorepo projects")

    # Tag both projects
    run_git(tmp_path, "tag", "mypkg-internal@v0.1.0")
    run_git(tmp_path, "tag", "mypkg-regular@v0.1.0")

    yield SimpleNamespace(
        root=tmp_path,
        projects=projects,
        dev_node_dir=dev_node_dir,
        regular_dir=regular_dir,
    )


class TestDevNodeProjectChecks:
    """Dev node projects should skip changelog-coverage and changelog-user-facing checks."""

    def test_dev_node_project_skips_user_facing_check(self, dev_node_monorepo):
        """Changelog-user-facing check returns skip for dev node projects."""
        from strictcli import CheckResult
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace, resolve_project
        from rlsbl.workspace_graph import WorkspaceGraph

        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        # Load workspace state
        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        # Create a WorkspaceCheckContext pointing at the dev node project
        ctx = WorkspaceCheckContext(
            project_root=dev_node_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        # Import and call the check function
        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        # Collect registered checks
        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        # Run changelog-user-facing check
        result = registered_checks["changelog-user-facing"](ctx)
        assert result.status == "skip"
        assert "dev node" in result.message

    def test_dev_node_project_skips_coverage_check(self, dev_node_monorepo):
        """Changelog-coverage check returns skip for dev node projects."""
        from strictcli import CheckResult
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        ctx = WorkspaceCheckContext(
            project_root=dev_node_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        result = registered_checks["changelog-coverage"](ctx)
        assert result.status == "skip"
        assert "dev node" in result.message

    def test_regular_project_not_skipped(self, dev_node_monorepo):
        """Changelog checks are NOT skipped for regular (non-dev-node) projects."""
        from rlsbl.check_context import WorkspaceCheckContext
        from rlsbl.workspace import load_workspace
        from rlsbl.workspace_graph import WorkspaceGraph

        root = dev_node_monorepo.root
        regular_dir = dev_node_monorepo.regular_dir

        projects = load_workspace(str(root))
        graph = WorkspaceGraph(str(root), projects)

        ctx = WorkspaceCheckContext(
            project_root=regular_dir,
            workspace_root=root,
            config={},
            projects=projects,
            graph=graph,
        )

        from rlsbl.checks import register_checks
        from unittest.mock import MagicMock

        mock_app = MagicMock()
        mock_app._checks_enabled = True

        registered_checks = {}
        def fake_check(name):
            def decorator(func):
                registered_checks[name] = func
                return func
            return decorator
        mock_app.check = fake_check

        register_checks(mock_app)

        # Coverage check should NOT skip for regular project
        result = registered_checks["changelog-coverage"](ctx)
        assert result.status != "skip" or "dev node" not in result.message

        # User-facing check should NOT skip for regular project
        result = registered_checks["changelog-user-facing"](ctx)
        assert result.status != "skip" or "dev node" not in result.message


class TestDevNodeChangelogAdd:
    """Dev node projects must reject changelog add commands."""

    def test_changelog_add_errors_in_dev_node(self, dev_node_monorepo):
        """cmd_add should hard-error when called in a dev_node project."""
        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        # Make a commit so we have a valid hash to pass
        sha = make_commit(root, "internal-pkg/new.js", "dev node feature")

        with pytest.raises(SystemExit) as exc_info:
            cmd_add(
                {
                    "commits": sha,
                    "description": "Should not work",
                    "type": "feature",
                },
                project_root=str(dev_node_dir),
            )

        assert exc_info.value.code == 1


class TestDevNodeReleaseSkipsChangelog:
    """Dev node projects must be rejected by the release guard."""

    def test_dev_node_release_skips_changelog(self, dev_node_monorepo, capsys):
        """Release run on a dev_node project hard-errors before any work."""
        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        # Make a commit so there's something to release
        make_commit(root, "internal-pkg/feature.js", "dev node work")

        # Create a release file and commit it so the tree is clean
        releases_dir = dev_node_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
            'description = "Internal update"\n'
        )
        run_git(root, "add", "internal-pkg/.rlsbl/releases/unreleased.toml")
        run_git(root, "commit", "-q", "-m", "add release file",
                "--trailer", "Autogenerated: true")

        ctx = ProjectContext(
            project_root=Path(str(dev_node_dir)),
            workspace_root=Path(str(root)),
            config={"private": False, "pipelines": {}},
        )

        rc = ReleaseConfig(bump="patch", include=["npm"], exclude=[],
                           description="Internal update")

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        ):
            with pytest.raises(SystemExit) as exc_info:
                release_run_cmd(rc, {"yes": True, "quiet": True}, ctx=ctx)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "dev_node projects cannot be released" in captured.err


class TestDevNodeReleaseRequiresDescription:
    """Dev node releases are blocked by the hard error guard regardless of config."""

    def test_dev_node_release_errors_without_description(self, dev_node_monorepo, capsys):
        """Release run on a dev_node project hard-errors before description check."""
        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        make_commit(root, "internal-pkg/feature.js", "dev node work")

        releases_dir = dev_node_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
            'description = ""\n'
        )
        run_git(root, "add", "internal-pkg/.rlsbl/releases/unreleased.toml")
        run_git(root, "commit", "-q", "-m", "add release file",
                "--trailer", "Autogenerated: true")

        ctx = ProjectContext(
            project_root=Path(str(dev_node_dir)),
            workspace_root=Path(str(root)),
            config={"private": False, "pipelines": {}},
        )

        rc = ReleaseConfig(bump="patch", include=["npm"], exclude=[], description="")

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        ):
            with pytest.raises(SystemExit) as exc_info:
                release_run_cmd(rc, {"yes": True, "quiet": True}, ctx=ctx)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "dev_node projects cannot be released" in captured.err

    def test_dev_node_release_body_includes_context(self, dev_node_monorepo, capsys):
        """Release run on a dev_node project hard-errors even with description and context."""
        root = dev_node_monorepo.root
        dev_node_dir = dev_node_monorepo.dev_node_dir

        make_commit(root, "internal-pkg/feature.js", "dev node work")

        releases_dir = dev_node_dir / ".rlsbl" / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        (releases_dir / "unreleased.toml").write_text(
            'bump = "patch"\ninclude = ["npm"]\nexclude = []\n'
            'description = "Updated internal deps"\n'
            'context = "Needed for dashboard v3"\n'
        )
        run_git(root, "add", "internal-pkg/.rlsbl/releases/unreleased.toml")
        run_git(root, "commit", "-q", "-m", "add release file",
                "--trailer", "Autogenerated: true")

        ctx = ProjectContext(
            project_root=Path(str(dev_node_dir)),
            workspace_root=Path(str(root)),
            config={"private": False, "pipelines": {}},
        )

        rc = ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="Updated internal deps",
            context="Needed for dashboard v3",
        )

        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        ):
            with pytest.raises(SystemExit) as exc_info:
                release_run_cmd(rc, {"yes": True, "quiet": True}, ctx=ctx)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "dev_node projects cannot be released" in captured.err


class TestReleaseEditDevNode:
    """release edit must reject dev_node projects with a hard error."""

    def test_release_edit_rejects_dev_node(self, dev_node_monorepo, capsys):
        """release edit on a dev_node project hard-errors before any work."""
        dev_node_dir = dev_node_monorepo.dev_node_dir

        with (
            patch("rlsbl.commands.edit_release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.edit_release.check_gh_auth", return_value=True),
        ):
            with pytest.raises(SystemExit) as exc_info:
                edit_run_cmd([], {"dry-run": False}, project_root=Path(str(dev_node_dir)))
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "dev_node projects cannot be released and have no release to edit" in captured.err
