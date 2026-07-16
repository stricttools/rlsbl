"""Tests for workspace-root error messages on per-sub-project commands.

When a per-sub-project command (status, changelog add, release init, etc.)
is run from the monorepo workspace root, the error should say "cd into a
sub-project" rather than the misleading "run 'rlsbl monorepo add'" -- the
workspace root is not an unregistered project.
"""

import json
import subprocess

from rlsbl import app
from rlsbl.workspace import save_workspace


def _git(repo, *args):
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _setup_monorepo(root):
    """Create a minimal monorepo workspace with one member."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    core = root / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text(
        '[project]\nname = "core"\nversion = "0.1.0"\n'
    )
    (core / ".rlsbl").mkdir()
    (core / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"], "pipelines": {}})
        + "\n"
    )
    (core / ".rlsbl" / "changes").mkdir()
    (core / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")

    save_workspace(
        str(root),
        [{"path": "core", "name": "core"}],
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


class TestWorkspaceRootUX:
    """Commands run from the workspace root should produce workspace-specific
    error messages, not the generic 'run rlsbl monorepo add'."""

    def test_status_at_workspace_root_says_cd_into_subproject(self, tmp_project):
        _setup_monorepo(tmp_project)
        result = app.test(["status"])
        assert result.exit_code != 0
        # Must NOT say "rlsbl monorepo add" -- the root is not unregistered
        assert "monorepo add" not in result.stderr
        # Must say something about workspace root / sub-project
        assert "workspace root" in result.stderr or "sub-project" in result.stderr

    def test_changelog_add_at_workspace_root_says_cd_into_subproject(
        self, tmp_project
    ):
        _setup_monorepo(tmp_project)
        result = app.test(
            ["changelog", "add", "--commits", "abc123", "--no-user-facing"]
        )
        assert result.exit_code != 0
        assert "monorepo add" not in result.stderr
        assert "workspace root" in result.stderr or "sub-project" in result.stderr

    def test_release_init_at_workspace_root_says_cd_into_subproject(
        self, tmp_project
    ):
        _setup_monorepo(tmp_project)
        result = app.test(["release", "init"])
        assert result.exit_code != 0
        assert "monorepo add" not in result.stderr
        assert "workspace root" in result.stderr or "sub-project" in result.stderr
