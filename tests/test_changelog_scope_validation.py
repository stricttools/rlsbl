"""Tests for write-time project scope validation in changelog add/amend."""

import json
import os
from pathlib import Path

import pytest

from conftest import git_head, make_commit, make_workspace, run_git, workspace_toml
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

        # A member's entries live under its releasable, not the package.
        from rlsbl.changelog.files import read_unreleased
        from rlsbl.workspace import get_releasable_changes_dir
        entries = read_unreleased(get_releasable_changes_dir(str(root), "alpha"))
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
        assert "does not touch files owned by" in captured.err
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


# ---------------------------------------------------------------------------
# The repo's FIRST commit is in scope like any other
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_releasable_monorepo(tmp_path, monkeypatch):
    """A monorepo whose INITIAL commit already contains a releasable's files.

    This is the state every new repo is in when it runs its very first
    ``rlsbl changelog add``: there is exactly one commit, it has no parent,
    and it created the whole project.
    """
    monkeypatch.chdir(tmp_path)

    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@test.local")
    run_git(tmp_path, "config", "user.name", "Test")

    ws_dir = tmp_path / WORKSPACE_DIR
    ws_dir.mkdir()
    (ws_dir / "workspace.toml").write_text(
        workspace_toml('[[projects]]\n'
        'path = "python"\n'
        'name = "pylib"\n'
        'releasable = "core"\n'
        '\n'
        '[[releasables]]\n'
        'name = "core"\n'
        'tag_format = "{name}@v{version}"\n')
    )
    rel_changes = ws_dir / "releasables" / "core" / "changes"
    rel_changes.mkdir(parents=True)
    (rel_changes / "unreleased.jsonl").write_text("")
    (ws_dir / "releasables" / "core" / "version").write_text("0.1.0\n")
    (ws_dir / "releasables" / "core" / "config.json").write_text(
        json.dumps({"publish_mode": "ci"}) + "\n"
    )

    pkg = tmp_path / "python"
    (pkg / "src").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "pylib"\nversion = "0.1.0"\n'
    )
    (pkg / "src" / "mod.py").write_text("VALUE = 1\n")

    run_git(tmp_path, "add", "-A")
    run_git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


class TestRootCommitIsInScope:
    """A parentless commit that created the project must not be "out of scope"."""

    def _root_sha(self, root):
        from githarness import git as _git
        return _git(root, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]

    def test_git_util_sees_the_root_commit_files(self, fresh_releasable_monorepo,
                                                 monkeypatch):
        from rlsbl.git_util import filter_commits_for_scope, get_commit_files
        from rlsbl.ownership import OwnershipScope

        root = fresh_releasable_monorepo
        monkeypatch.chdir(root)
        sha = self._root_sha(root)

        files = get_commit_files(sha)
        assert "python/pyproject.toml" in files
        assert "python/src/mod.py" in files

        proj = {"path": "python", "name": "pylib"}
        members = [{"path": ".", "name": "root"}, proj]
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_member(members, proj), operation="test",
        ) == {sha}
        assert filter_commits_for_scope(
            {sha}, OwnershipScope.for_members(members, [proj]), operation="test",
        ) == {sha}

    def test_changelog_add_accepts_the_root_commit(self, fresh_releasable_monorepo,
                                                   monkeypatch):
        root = fresh_releasable_monorepo
        pkg = root / "python"
        monkeypatch.chdir(pkg)
        sha = self._root_sha(root)

        flags = {
            "commits": sha[:12],
            "description": "First release of the library",
            "type": "feature",
            "user-facing": True,
            "auto-commit": False,
        }
        cmd_add(flags, project_root=pkg)

        from rlsbl.changelog.files import read_unreleased
        from rlsbl.workspace import get_releasable_changes_dir

        entries = read_unreleased(get_releasable_changes_dir(str(root), "core"))
        assert len(entries) == 1
        assert entries[0].description == "First release of the library"

    def test_out_of_scope_root_commit_is_still_rejected(self, tmp_path,
                                                        monkeypatch):
        """The fix must not turn scope validation into a rubber stamp."""
        monkeypatch.chdir(tmp_path)

        run_git(tmp_path, "init", "-q", "-b", "main")
        run_git(tmp_path, "config", "user.email", "test@test.local")
        run_git(tmp_path, "config", "user.name", "Test")

        projects = [{"path": "alpha", "name": "alpha"},
                    {"path": "beta", "name": "beta"}]
        make_workspace(tmp_path, projects)
        for proj in ("alpha", "beta"):
            d = tmp_path / proj
            (d / ".rlsbl" / "changes").mkdir(parents=True)
            (d / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
            (d / ".rlsbl" / "config.json").write_text(
                json.dumps({"publish_mode": "ci"}) + "\n"
            )
        # The root commit creates ONLY beta.
        (tmp_path / "beta" / "src.py").write_text("x = 1\n")
        run_git(tmp_path, "add", WORKSPACE_DIR, "beta")
        run_git(tmp_path, "commit", "-q", "-m", "initial")
        sha = git_head(tmp_path)

        alpha_dir = tmp_path / "alpha"
        monkeypatch.chdir(alpha_dir)
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


class TestScopeCarriesTheWholeMemberList:
    """A changelog's scope is asked against every member, never a subset.

    ``OwnershipScope`` answers "who owns this file?" against the whole
    workspace and only then asks whether that owner is in scope.  Handing it
    the in-scope members alone is the mis-attribution the class exists to
    prevent: a member at ``pkg`` would claim ``pkg/inner/x.py`` because the
    member at ``pkg/inner`` was not in the list to outrank it.
    """

    def test_the_member_list_is_mandatory(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        with pytest.raises(TypeError):
            _ResolvedContext(project={"path": "pkg", "name": "pkg"})

    def test_scope_attributes_against_every_member(self):
        from rlsbl.commands.changelog_cmd import _ResolvedContext

        pkg = {"path": "pkg", "name": "pkg"}
        inner = {"path": "pkg/inner", "name": "inner"}
        ctx = _ResolvedContext(
            project=pkg,
            member_projects=[pkg],
            all_projects=[{"path": ".", "name": "root"}, pkg, inner],
        )
        scope = ctx.scope()
        assert scope.owner_name_of("pkg/inner/x.py") == "inner"
        assert scope.claims("pkg/inner/x.py") is False
        assert scope.claims("pkg/x.py") is True

    def test_an_empty_member_list_is_refused(self):
        """No silent substitution of the in-scope members for the real list."""
        from rlsbl.commands.changelog_cmd import _ResolvedContext
        from rlsbl.ownership import OwnershipError

        pkg = {"path": "pkg", "name": "pkg"}
        ctx = _ResolvedContext(project=pkg, member_projects=[pkg], all_projects=[])
        with pytest.raises(OwnershipError):
            ctx.scope()
