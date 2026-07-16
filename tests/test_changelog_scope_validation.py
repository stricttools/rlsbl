"""Tests for write-time project scope validation in changelog add/amend."""

import json
import os
from pathlib import Path

import pytest

from conftest import run_git, make_commit, make_workspace
from rlsbl.commands.changelog_cmd import cmd_add
from rlsbl.workspace import WORKSPACE_DIR


@pytest.fixture
def monorepo_with_projects(tmp_path, monkeypatch):
    """Create a monorepo with two subprojects for scope validation tests.

    Project 'alpha' at alpha/ and project 'beta' at beta/.
    Commits to alpha/ should only be added to alpha's changelog, not beta's.
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    readme = tmp_path / "README.md"
    readme.write_text("# monorepo\n")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "initial")

    projects = [
        {"path": "alpha", "name": "alpha"},
        {"path": "beta", "name": "beta"},
    ]
    make_workspace(tmp_path, projects)

    # Set up both subprojects with changelog infrastructure
    for proj in ["alpha", "beta"]:
        proj_dir = tmp_path / proj
        (proj_dir / ".rlsbl" / "changes").mkdir(parents=True)
        (proj_dir / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
        (proj_dir / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci"}) + "\n"
        )
        (proj_dir / "package.json").write_text(
            json.dumps({"name": proj, "version": "0.1.0"})
        )

    run_git(tmp_path, "add", WORKSPACE_DIR)
    run_git(tmp_path, "add", "alpha")
    run_git(tmp_path, "add", "beta")
    run_git(tmp_path, "commit", "-q", "-m", "add projects")

    run_git(tmp_path, "tag", "alpha@v0.1.0")
    run_git(tmp_path, "tag", "beta@v0.1.0")

    return tmp_path


class TestChangelogAddValidatesProjectScope:
    """Commits that touch project files succeed in changelog add."""

    def test_in_scope_commit_succeeds(self, monorepo_with_projects, monkeypatch):
        root = monorepo_with_projects
        alpha_dir = root / "alpha"
        monkeypatch.chdir(alpha_dir)

        # Make a commit that touches alpha/
        sha = make_commit(root, "alpha/src.py", "alpha change")

        flags = {
            "commits": sha[:12],
            "description": "Alpha feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        # Should succeed without error
        cmd_add(flags, project_root=alpha_dir)

        from rlsbl.changelog.files import get_changes_dir, read_unreleased
        entries = read_unreleased(get_changes_dir(str(alpha_dir)))
        assert len(entries) == 1
        assert entries[0].description == "Alpha feature"


class TestChangelogAddRejectsOutOfScopeCommit:
    """Commits that don't touch project files cause a hard error."""

    def test_out_of_scope_commit_rejected(self, monorepo_with_projects, monkeypatch):
        root = monorepo_with_projects
        alpha_dir = root / "alpha"
        monkeypatch.chdir(alpha_dir)

        # Make a commit that only touches beta/
        sha = make_commit(root, "beta/src.py", "beta change")

        flags = {
            "commits": sha[:12],
            "description": "Should fail",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit) as exc_info:
            cmd_add(flags, project_root=alpha_dir)
        assert exc_info.value.code == 1

    def test_error_message_includes_project_info(
        self, monorepo_with_projects, monkeypatch, capsys
    ):
        root = monorepo_with_projects
        alpha_dir = root / "alpha"
        monkeypatch.chdir(alpha_dir)

        sha = make_commit(root, "beta/src.py", "beta change")

        flags = {
            "commits": sha[:12],
            "description": "Should fail",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        with pytest.raises(SystemExit):
            cmd_add(flags, project_root=alpha_dir)

        captured = capsys.readouterr()
        assert "does not touch files in project" in captured.err
        assert "'alpha'" in captured.err
        assert "workspace.toml" in captured.err


class TestChangelogAddSkipsValidationStandalone:
    """Standalone projects (no monorepo) skip scope validation entirely."""

    def test_standalone_no_scope_check(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "test@test.local")
        run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-q", "-m", "initial")
        run_git(repo, "tag", "v0.0.0")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        sha = make_commit(repo)

        flags = {
            "commits": sha[:12],
            "description": "Standalone feature",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        # Should succeed -- no workspace means no scope validation
        cmd_add(flags, project_root=repo)

        from rlsbl.changelog.files import get_changes_dir, read_unreleased
        entries = read_unreleased(get_changes_dir(str(repo)))
        assert len(entries) == 1
