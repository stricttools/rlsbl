"""Regression tests for plain-target scaffold root resolution.

Bug 1: when running `rlsbl scaffold --target plain` in a monorepo sub-project,
`detect_registries()` returns empty (PlainTarget.detect() always returns
False), so cmd_scaffold falls through to `find_project_root()` which walks
up and finds the monorepo root. The scaffold_root is then the monorepo root
instead of cwd, causing `_is_non_releasable_project()` to fail (it can't
resolve the project from the wrong root). This means changelog infrastructure
(unreleased.jsonl, CHANGELOG.md) gets created for non-releasable projects
that should not have it.

Fix 1: when --target is explicitly passed, always use Path.cwd() as
scaffold_root.

Bug 2: bare `rlsbl scaffold` (no --target) fails for already-scaffolded
plain-target projects because detect_registries() returns empty (PlainTarget
never auto-detects) and the code errors with "no package.json, pyproject.toml,
or go.mod found" even though .rlsbl/config.json has targets: ["plain"].

Fix 2: when cwd has .rlsbl/config.json, use cwd as scaffold_root and read
targets from config when detect_registries() is empty.
"""

import json

import pathlib


from rlsbl.commands.init_cmd import run_cmd, _is_non_releasable_project
from rlsbl.context import create_context
from conftest import make_workspace


class TestScaffoldPlainDevNode:
    """Scaffolding a plain-target dev_node project must skip changelog files."""

    def _setup_monorepo_with_dev_node(self, mock_git_repo, subdir="infra"):
        """Create a monorepo with a plain dev_node sub-project.

        Returns the sub-project directory path.
        """
        proj_dir = mock_git_repo / subdir
        proj_dir.mkdir()

        # Set up workspace.toml with the project marked as dev_node
        make_workspace(mock_git_repo, [
            {"path": subdir, "name": subdir, "dev_only": True, "releasable": False},
        ])

        return proj_dir

    def test_is_non_releasable_with_correct_root(self, mock_git_repo, monkeypatch):
        """_is_non_releasable_project returns True when project_root points to
        the sub-project directory (the fixed behavior)."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # With project_root = sub-project dir (correct), non-releasable is detected
        assert _is_non_releasable_project(proj_dir) is True

    def test_is_non_releasable_at_the_workspace_root(self, mock_git_repo, monkeypatch):
        """At the workspace root, the answer is about the root member.

        The workspace root used to match no member at all, so the answer was
        False whatever the workspace said. It is the root member's directory,
        and the default root member is a dev node -- so the answer is True.
        """
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        assert _is_non_releasable_project(mock_git_repo) is True

    def test_scaffold_plain_dev_node_no_changelog(self, mock_git_repo, monkeypatch):
        """Scaffolding a plain dev_node project must NOT create changelog files."""
        proj_dir = self._setup_monorepo_with_dev_node(mock_git_repo)
        monkeypatch.chdir(proj_dir)

        # Create context with project_root = sub-project dir (the fix)
        ctx = create_context(proj_dir)

        # Run scaffold for the plain target
        run_cmd("plain", [], {
            "auto-commit": False,
            "auto-tag": False,
            "skip-shared": False,
        }, ctx=ctx)

        # Dev node projects must NOT have changelog infrastructure
        changelog = proj_dir / "CHANGELOG.md"
        unreleased = proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl"

        assert not changelog.exists(), (
            "CHANGELOG.md should not be created for dev_node projects"
        )
        assert not unreleased.exists(), (
            "unreleased.jsonl should not be created for dev_node projects"
        )

    def test_scaffold_plain_non_dev_node_has_changelog(self, mock_git_repo, monkeypatch):
        """Scaffolding a plain NON-dev_node project creates changelog files normally."""
        proj_dir = mock_git_repo / "lib"
        proj_dir.mkdir()

        # Set up workspace without dev_node flag
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
        ])

        monkeypatch.chdir(proj_dir)
        ctx = create_context(proj_dir)

        run_cmd("plain", [], {
            "auto-commit": False,
            "auto-tag": False,
            "skip-shared": False,
        }, ctx=ctx)

        # A releasable member's changelog lives under its releasable, not the
        # package -- so that is where scaffolding puts it.
        from rlsbl.workspace import get_releasable_changes_dir, get_releasable_dir

        rel_dir = pathlib.Path(get_releasable_dir(str(mock_git_repo), "lib"))
        unreleased = pathlib.Path(
            get_releasable_changes_dir(str(mock_git_repo), "lib"),
            "unreleased.jsonl",
        )
        assert unreleased.exists(), (
            "unreleased.jsonl should be created for non-dev_node projects"
        )
        assert rel_dir.is_dir(), (
            "the releasable's state directory should be created"
        )


class TestBareScaffoldPlainTarget:
    """Bare `rlsbl scaffold` (no --target) for already-scaffolded plain projects."""

    def test_bare_scaffold_reads_target_from_config(self, mock_git_repo, monkeypatch):
        """Already-scaffolded plain-target project re-scaffolds without --target.

        When .rlsbl/config.json exists with targets: ["plain"], bare scaffold
        should use cwd as scaffold_root and read the target from config instead
        of erroring with 'no package.json found'.
        """
        # Set up a plain-target project that was previously scaffolded
        rlsbl_dir = mock_git_repo / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(
            json.dumps({"targets": ["plain"], "publish_mode": "ci"})
        )
        (mock_git_repo / "VERSION").write_text("0.1.0\n")
        monkeypatch.chdir(mock_git_repo)

        # Run bare scaffold (no --target flag) via app.test
        from rlsbl import app
        app.test(["scaffold", "--no-auto-tag"])

        # Verify .rlsbl/version was written (proves scaffold ran to completion)
        from rlsbl import __version__
        version_marker = rlsbl_dir / "version"
        assert version_marker.exists(), (
            ".rlsbl/version should be created by scaffold"
        )
        assert version_marker.read_text().strip() == __version__

    def test_bare_scaffold_plain_without_config_still_errors(self, tmp_project):
        """Without .rlsbl/config.json and no manifest files, scaffold errors."""
        import subprocess
        result = subprocess.run(
            # No confirm-skip flag: `scaffold` is `mutating` but not
            # `consequential`, so the framework never prompts for it.
            ["python", "-m", "rlsbl", "scaffold"],
            capture_output=True, text=True,
            cwd=str(tmp_project),
        )
        assert result.returncode != 0
        assert "no package.json" in result.stderr or "not in an rlsbl project" in result.stderr


class TestDevNodeGetsNoPublishWorkflow:
    """Dev nodes cannot be released, so they get no publish workflow.

    Regression: publish scaffolding was gated only on ``publish_mode`` and the
    workspace-root check, so a dev node carrying the ordinary scaffold default
    ``publish_mode: "ci"`` got a ``publish.yml`` that can never legitimately
    run -- ``rlsbl release run`` hard-errors on a dev node.
    """

    def _dev_node_npm_project(self, mock_git_repo, *, dev_node=True,
                              subdir="conformance"):
        proj_dir = mock_git_repo / subdir
        proj_dir.mkdir()
        (proj_dir / "package.json").write_text(json.dumps({
            "name": "conformance", "version": "0.1.0",
            "scripts": {"test": "node t.js"},
        }, indent=2) + "\n")
        rlsbl_dir = proj_dir / ".rlsbl"
        rlsbl_dir.mkdir()
        (rlsbl_dir / "config.json").write_text(json.dumps({
            "targets": ["npm"], "publish_mode": "ci",
        }, indent=2) + "\n")
        make_workspace(mock_git_repo, [
            dict({"path": subdir, "name": subdir},
                 **({"dev_only": True, "releasable": False} if dev_node else {})),
        ])
        return proj_dir

    def _scaffold(self, proj_dir):
        run_cmd("npm", [], {
            "auto-commit": False, "auto-tag": False, "skip-shared": True,
        }, ctx=create_context(proj_dir))

    def test_no_publish_workflow_for_dev_node(self, mock_git_repo, monkeypatch,
                                              capsys):
        proj_dir = self._dev_node_npm_project(mock_git_repo)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)

        assert not (proj_dir / ".github" / "workflows" / "publish.yml").exists()
        # CI is still scaffolded -- a dev node is tested, just never released.
        assert (proj_dir / ".github" / "workflows" / "ci.yml").exists()

    def test_releasable_project_still_gets_one(self, mock_git_repo, monkeypatch,
                                               capsys):
        proj_dir = self._dev_node_npm_project(mock_git_repo, dev_node=False)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)

        assert (proj_dir / ".github" / "workflows" / "publish.yml").exists()

    def test_existing_publish_workflow_is_swept(self, mock_git_repo, monkeypatch,
                                                capsys):
        """A dev node scaffolded before the fix loses its publish.yml on re-scaffold."""
        proj_dir = self._dev_node_npm_project(mock_git_repo, dev_node=False)
        monkeypatch.chdir(proj_dir)
        self._scaffold(proj_dir)
        publish = proj_dir / ".github" / "workflows" / "publish.yml"
        assert publish.exists()

        # The project is (re)declared a dev node.
        make_workspace(mock_git_repo, [
            {"path": "conformance", "name": "conformance", "dev_only": True, "releasable": False},
        ])
        self._scaffold(proj_dir)
        assert not publish.exists()
