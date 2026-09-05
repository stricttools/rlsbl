"""Tests for what per-member commands do at the monorepo workspace root.

The workspace root is the root member's own directory -- every workspace
declares one -- so a per-member command run there resolves to the root member
and reports on it. What it must never do is claim the directory is an
unregistered project and tell the operator to run `rlsbl monorepo add`.
"""

import json
import subprocess

from rlsbl import app

from conftest import make_workspace


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

    make_workspace(str(root), [{"path": "core", "name": "core"}])

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


class TestWorkspaceRootUX:
    """Commands run from the workspace root resolve to the root member.

    The default root member here is a dev node, so each command refuses for a
    reason that names the root member's own kind -- never "run rlsbl monorepo
    add", which would be false: the directory IS registered.
    """

    def test_status_at_workspace_root_reports_the_root_member(self, tmp_project):
        _setup_monorepo(tmp_project)
        result = app.test(["status"])
        assert result.exit_code != 0
        # Must NOT say "rlsbl monorepo add" -- the root is not unregistered
        assert "monorepo add" not in result.stderr

    def test_changelog_add_at_workspace_root_names_the_root_members_kind(
        self, tmp_project
    ):
        _setup_monorepo(tmp_project)
        result = app.test(
            ["changelog", "add", "--commits", "abc123", "--no-user-facing"]
        )
        assert result.exit_code != 0
        assert "monorepo add" not in result.stderr
        # The default root member is a dev node, so it has no changelog.
        assert "non-releasable" in result.stderr

    def test_release_init_at_workspace_root_does_not_claim_unregistered(
        self, tmp_project
    ):
        _setup_monorepo(tmp_project)
        result = app.test(["release", "init"])
        assert result.exit_code != 0
        assert "monorepo add" not in result.stderr
